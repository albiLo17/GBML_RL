

class BatchData:  # batchdata collected from policy
    def __init__(self):
        self.states = []
        self.actions = []
        self.v = []
        self.logprobs = []  # log probs of each action
        self.old_logprobs = []
        self.rewards = []
        self.is_terminal = []  # whether or not terminal state was reached
        self.next_states = []

    def clear(self):
        self.states.clear()
        self.actions.clear()
        self.logprobs.clear()
        self.old_logprobs.clear()
        self.v.clear()
        self.rewards.clear()
        self.is_terminal.clear()
        self.next_states.clear()


def calc_rtg(rewards, is_terminals, gamma):
    # Calculates reward-to-go
    assert len(rewards) == len(is_terminals)
    rtgs = []
    discounted_reward = 0
    for reward, is_terminal in zip(reversed(rewards), reversed(is_terminals)):
        if is_terminal:
            discounted_reward = 0
        discounted_reward = reward + gamma * discounted_reward
        rtgs.insert(0, discounted_reward)
    return rtgs