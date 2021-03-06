import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
import random
from networks import MamlParamsPg, MamlParamsPPO
from agents.pg_agent import BatchData, calc_rtg
from networks import ActorCritic, Actor
import numpy as np
from torch.optim import Adam, RMSprop, SGD

"""

PPO meta-agent

"""

class PPO(nn.Module):
    def __init__(self, params, logdir, device, adaptive_lr=False, load_pretrained=False):
        super(PPO, self).__init__()
        # extract environment info from maze....
        self.state_dim = 3  # {obstacles, current position, goal position}
        self.action_dim = 4  # {0: Down, 1: Up, 2: Right, 3: Left}
        self.batchdata = [[BatchData() for _ in range(params['batch_tasks'])], [BatchData() for _ in range(params['batch_tasks'])]]        # list[0] for adaptation batch and list[1] for evaluation batch
        self.writer = SummaryWriter(log_dir=logdir)
        self.log_idx = 0
        self.log_grads_idx = 0
        self.device = device

        self.inner_lr = 0.1

        self.epsilon0 = 0.9
        self.epsilon = self.epsilon0
        self.epsilon_adapt = params['eps_adapt'] #1.0 #0.5
        self.epsilon_decay = 0.99 #0.85

        # Init params and actor-critic policy networks, old_policy used for sampling, policy for training
        self.lr = 0.0001
        self.eps_clip = 0.1
        self.gamma = 0.9
        self.c1 = params['c1']
        self.c2 = params['c2']

        self.ADAPTATION = 0
        self.EVALUATION = 1

        self.norm_A = params['norm_A']

        self.policy = MamlParamsPPO(params['grid_size'], self.inner_lr, model_type=params['model_type'], activation=params['activation'], adaptive_lr=adaptive_lr)

        self.grads_vals = np.zeros(len(self.policy.get_theta()))

        self.MSE_loss = nn.MSELoss()  # to calculate critic loss
        self.optimizer = Adam(self.policy.parameters(), lr=self.lr, eps=params['adam_epsilon'])

    def get_epsilon(self, mode=0):
        if mode == self.ADAPTATION:
            return self.epsilon_adapt
        else:
            return self.epsilon0

    def update_epsilon(self, mode):
        if mode == self.EVALUATION:
            self.epsilon *= self.epsilon_decay

    def get_action(self, state, theta=None, test=False):
        # Sample actions with epsilon greedy
        if np.random.random() > self.epsilon or test:
            a, log_prob, v = self.policy(self.to_tensor(state), theta=theta)
            return a, log_prob, v
        else:
            a = np.random.randint(0, self.action_dim)
            log_prob, v, _ = self.policy.evaluate(self.to_tensor(state), a, theta=theta)
            return a, log_prob, v

    def adapt(self, idx, train=False, print_grads=False):

        self.policy.train()

        theta_i = self.policy.get_theta()

        rtgs = self.to_tensor(calc_rtg(self.batchdata[0][idx].rewards, self.batchdata[0][idx].is_terminal, self.gamma))  # reward-to-go
        # Normalize rewards
        logprobs = torch.cat([x.view(1) for x in self.batchdata[0][idx].logprobs])
        v = torch.cat([x.view(1) for x in self.batchdata[0][idx].v])

        loss_pi = (- rtgs * logprobs).mean()
        loss_v = (self.c1*(v - rtgs)**2).mean()
        loss = loss_pi + loss_v

        if train:
            theta_grad_s = torch.autograd.grad(outputs=loss, inputs=theta_i, create_graph=True)
            theta_i = list(map(lambda p: p[1] - p[2] * p[0], zip(theta_grad_s, theta_i, self.policy.lr)))

            if print_grads:
                for i, grad in enumerate(theta_grad_s):
                    self.grads_vals[i] += torch.mean(torch.abs(grad))

        else:
            theta_grad_s = torch.autograd.grad(outputs=loss, inputs=theta_i)
            theta_i = list(map(lambda p: p[1] - p[2] * p[0].detach(), zip(theta_grad_s, theta_i, self.policy.lr)))

        return theta_i, loss.detach().cpu().item(), loss_pi.detach().cpu().item(), loss_v.detach().cpu().item()

    def update_adaptation_batches(self):

        for batchdata in (self.batchdata[0]):
            states = torch.cat([self.to_tensor(x) for x in batchdata.states], 0).detach()
            actions = self.to_tensor(batchdata.actions).long().detach()
            logprobs, state_vals, _ = self.policy.evaluate(states, actions)

            batchdata.logprobs = [x for x in logprobs]
            batchdata.v = [x for x in state_vals]

    def get_loss(self, theta_i, idx):
        # get form correct batch old policy data
        rtgs = self.to_tensor(calc_rtg(self.batchdata[1][idx].rewards, self.batchdata[1][idx].is_terminal, self.gamma))  # reward-to-go

        old_states = torch.cat([self.to_tensor(x) for x in self.batchdata[1][idx].states], 0).detach()
        old_actions = self.to_tensor(self.batchdata[1][idx].actions).long().detach()
        old_logprobs = torch.cat([x.view(1) for x in self.batchdata[1][idx].logprobs]).detach()

        #get form correct batch new policy data
        logprobs, state_vals, H = self.policy.evaluate(old_states, old_actions, theta=theta_i)

        # Compute loss
        # Importance ratio
        ratios = torch.exp(logprobs - old_logprobs.detach())  # new probs over old probs

        # Calc advantages
        A = rtgs - state_vals
        if self.norm_A == 1:
            A = ((A - torch.mean(A)) / torch.std(A)).detach()

        # Actor loss using CLIP loss
        surr1 = ratios * A
        surr2 = torch.clamp(ratios, 1 - self.eps_clip, 1 + self.eps_clip) * A
        actor_loss = torch.mean( - torch.min(surr1, surr2) )  # minus to maximize

        # Critic loss fitting to reward-to-go with entropy bonus
        critic_loss = self.c1 * self.MSE_loss(rtgs, state_vals)

        loss = actor_loss + critic_loss - self.c2 * H.mean()

        return loss




    def save_model(self, filepath='./ppo_model.pth'):  # TODO filename param
        torch.save(self.policy.state_dict(), filepath)

    def load_model(self, filepath='./ppo_model.pth'):
        self.policy.load_state_dict(torch.load(filepath))

    def write_reward(self, r, r2):
        """
        Function that write on tensorboard the rewards it gets

        :param r: cumulative reward of the episode
        :type r: float
        :param r2: final reword of the episode
        :type r2: float
        """
        self.writer.add_scalar('cumulative_reward', r, self.log_idx)
        self.writer.add_scalar('final_reward', r2, self.log_idx)
        self.log_idx += 1

    def push_batchdata(self, st, a, logprob, v, r, done, mode, idx):
        # adds a row of trajectory data to self.batchdata
        self.batchdata[mode][idx].states.append(st)
        self.batchdata[mode][idx].actions.append(a)
        self.batchdata[mode][idx].logprobs.append(logprob)
        self.batchdata[mode][idx].v.append(v)
        self.batchdata[mode][idx].rewards.append(r)
        self.batchdata[mode][idx].is_terminal.append(done)

    def clear_batchdata(self):
        for i in range(2):
            for batchdata in self.batchdata[i]:
                batchdata.clear()

    def to_tensor(self, array):
        if isinstance(array, np.ndarray):
            return torch.from_numpy(array).float().to(self.device)
        else:
            return torch.tensor(array, dtype=torch.float).to(self.device)


"""

REINFORCE meta-agent

"""
class REINFORCE(nn.Module):
    def __init__(self, params, logdir, device, load_pretrained=False):
        super(REINFORCE, self).__init__()
        # extract environment info from maze....
        self.state_dim = 3  # I guess for 1 grid image?
        self.action_dim = 4  # {0: Down, 1: Up, 2: Right, 3: Left}
        self.batchdata = BatchData()
        self.writer = SummaryWriter(log_dir=logdir)
        self.log_idx = 0
        self.debug_idx = 0
        self.device = device

        self.inner_lr = 0.1
        # self.training_steps = 10

        # Init params and actor-critic policy networks, old_policy used for sampling, policy for training
        self.lr = 0.001 #0.001  # 0.01
        self.gamma = 0.9
        self.epsilon0 = 0.9
        self.epsilon = self.epsilon0
        self.epsilon_adapt = 0.5
        self.epsilon_decay = 0.95

        self.c = params['entropy_bonus']

        self.policy = MamlParamsPg(params['grid_size'])

        self.MSE_loss = nn.MSELoss()  # to calculate critic loss
        self.optimizer = RMSprop(self.policy.parameters(), lr=self.lr)

    def get_action(self, state, theta=None, test=False):
        # Sample actions with epsilon greedy
        if np.random.random() > self.epsilon or test:
            a, log_prob, _ = self.policy(self.to_tensor(state), theta=theta)
            return a, log_prob, None
        else:
            a = np.random.randint(0, 3)
            log_prob, _ = self.policy.evaluate(self.to_tensor(state), a, theta=theta)
            return a, log_prob, None

    def adapt(self, train=False):

        self.policy.train()

        theta_i = self.policy.get_theta()

        rtgs = self.to_tensor(calc_rtg(self.batchdata.rewards, self.batchdata.is_terminal, self.gamma))  # reward-to-go
        # Normalize rewards
        # rtgs = (rtgs - rtgs.mean()) / (rtgs.std() + 1e-5)  # todo: ?
        logprobs = torch.cat([x.view(1) for x in self.batchdata.logprobs])

        # Normalize advantages
        # advantages = (A-A.mean()) / (A.std() + 1e-5)
        loss = (- rtgs * logprobs).mean()

        if train:
            theta_grad_s = torch.autograd.grad(outputs=loss, inputs=theta_i, create_graph=True)
            theta_i = list(map(lambda p: p[1] - p[2] * p[0], zip(theta_grad_s, theta_i, self.policy.lr)))
        else:
            theta_grad_s = torch.autograd.grad(outputs=loss, inputs=theta_i)
            theta_i = list(map(lambda p: p[1] - p[2] * p[0].detach(), zip(theta_grad_s, theta_i, self.policy.lr)))

            for i, grad in enumerate(theta_grad_s):
                self.writer.add_scalar('params_grad_' + str(i), torch.mean(torch.abs(grad)), self.debug_idx)
            self.debug_idx += 1

        return theta_i, loss.detach().cpu().item()

    def update(self, theta_i):
        """
            Updates the actor-critic networks for current batch data
        """
        rtgs = self.to_tensor(calc_rtg(self.batchdata.rewards, self.batchdata.is_terminal, self.gamma))  # reward-to-go
        # Normalize rewards
        # rtgs = ((rtgs - torch.mean(rtgs)) / torch.std(rtgs)).detach()

        logprobs = torch.cat([x.view(1) for x in self.batchdata.logprobs])

        loss = - rtgs*logprobs

        return loss.mean()

    # def regularize(self):
    #     actions = self.batchdata.actions
    #     states = torch.cat([self.to_tensor(x) for x in self.batchdata.states], 0)
    #     _, entropy = self.policy.evaluate(states, actions)
    #     loss = - entropy
    #
    #     return loss


    def save_model(self, actor_filepath='./ppo_actor.pth', critic_filepath='./ppo_critic.pth'):  # TODO filename param
        torch.save(self.policy.actor.state_dict(), actor_filepath)
        torch.save(self.policy.critic.state_dict(), critic_filepath)

    def load_model(self, actor_filepath='./ppo_actor.pth', critic_filepath='./ppo_critic.pth'):
        self.policy.actor.load_state_dict(torch.load(actor_filepath))
        self.policy.critic.load_state_dict(torch.load(critic_filepath))

    def write_reward(self, r, r2):
        """
        Function that write on tensorboard the rewards it gets

        :param r: cumulative reward of the episode
        :type r: float
        :param r2: final reword of the episode
        :type r2: float
        """
        self.writer.add_scalar('cumulative_reward', r, self.log_idx)
        self.writer.add_scalar('final_reward', r2, self.log_idx)
        self.log_idx += 1

    def push_batchdata(self, st, a, logprob, v, r, done):
        # adds a row of trajectory data to self.batchdata
        self.batchdata.states.append(st)
        self.batchdata.actions.append(a)
        self.batchdata.logprobs.append(logprob)
        # self.batchdata.v.append(v)
        self.batchdata.rewards.append(r)
        self.batchdata.is_terminal.append(done)

    def clear_batchdata(self):
        self.batchdata.clear()

    def to_tensor(self, array):
        if isinstance(array, np.ndarray):
            return torch.from_numpy(array).float().to(self.device)
        else:
            return torch.tensor(array, dtype=torch.float).to(self.device)