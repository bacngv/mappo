import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal
from torch.utils.data.sampler import BatchSampler, SequentialSampler

# Orthogonal initialization for a given layer's parameters
def orthogonal_init(layer, gain=1.0):
    for name, param in layer.named_parameters():
        if 'bias' in name:
            nn.init.constant_(param, 0)
        elif 'weight' in name:
            nn.init.orthogonal_(param, gain=gain)

# Actor and Critic Networks for Continuous Action Spaces 

class Actor_RNN(nn.Module):
    def __init__(self, args, actor_input_dim):
        super(Actor_RNN, self).__init__()
        self.rnn_hidden = None

        self.fc1 = nn.Linear(actor_input_dim, args.rnn_hidden_dim)
        self.rnn = nn.GRUCell(args.rnn_hidden_dim, args.rnn_hidden_dim)
        self.fc_mean = nn.Linear(args.rnn_hidden_dim, args.action_dim)
        self.fc_log_std = nn.Linear(args.rnn_hidden_dim, args.action_dim)
        # Choose activation function based on argument (0: Tanh, 1: ReLU)
        self.activate_func = [nn.Tanh(), nn.ReLU()][args.use_relu]

        if args.use_orthogonal_init:
            print("------use_orthogonal_init------")
            orthogonal_init(self.fc1)
            orthogonal_init(self.rnn)
            orthogonal_init(self.fc_mean, gain=0.01)
            orthogonal_init(self.fc_log_std, gain=0.01)

    def forward(self, actor_input):
        x = self.activate_func(self.fc1(actor_input))
        self.rnn_hidden = self.rnn(x, self.rnn_hidden)
        action_mean = torch.tanh(self.fc_mean(self.rnn_hidden))
        log_std = self.fc_log_std(self.rnn_hidden)
        log_std = torch.clamp(log_std, min=-20, max=2)
        return action_mean, log_std

class Actor_MLP(nn.Module):
    def __init__(self, args, actor_input_dim):
        super(Actor_MLP, self).__init__()
        self.fc1 = nn.Linear(actor_input_dim, args.mlp_hidden_dim)
        self.fc2 = nn.Linear(args.mlp_hidden_dim, args.mlp_hidden_dim)
        self.mean_layer = nn.Linear(args.mlp_hidden_dim, args.action_dim)
        self.log_std_layer = nn.Linear(args.mlp_hidden_dim, args.action_dim)
        self.activate_func = [nn.Tanh(), nn.ReLU()][args.use_relu]

        if args.use_orthogonal_init:
            print("------use_orthogonal_init------")
            orthogonal_init(self.fc1)
            orthogonal_init(self.fc2)
            orthogonal_init(self.mean_layer, gain=0.01)
            orthogonal_init(self.log_std_layer, gain=0.01)

    def forward(self, actor_input):
        x = self.activate_func(self.fc1(actor_input))
        x = self.activate_func(self.fc2(x))
        mean = self.mean_layer(x)
        log_std = self.log_std_layer(x)
        log_std = torch.clamp(log_std, min=-20, max=2)
        return mean, log_std

class Critic_RNN(nn.Module):
    def __init__(self, args, critic_input_dim):
        super(Critic_RNN, self).__init__()
        self.rnn_hidden = None

        self.fc1 = nn.Linear(critic_input_dim, args.rnn_hidden_dim)
        self.rnn = nn.GRUCell(args.rnn_hidden_dim, args.rnn_hidden_dim)
        self.fc2 = nn.Linear(args.rnn_hidden_dim, 1)
        self.activate_func = [nn.Tanh(), nn.ReLU()][args.use_relu]
        if args.use_orthogonal_init:
            print("------use_orthogonal_init------")
            orthogonal_init(self.fc1)
            orthogonal_init(self.rnn)
            orthogonal_init(self.fc2)

    def forward(self, critic_input):
        x = self.activate_func(self.fc1(critic_input))
        self.rnn_hidden = self.rnn(x, self.rnn_hidden)
        value = self.fc2(self.rnn_hidden)
        return value

class Critic_MLP(nn.Module):
    def __init__(self, args, critic_input_dim):
        super(Critic_MLP, self).__init__()
        self.fc1 = nn.Linear(critic_input_dim, args.mlp_hidden_dim)
        self.fc2 = nn.Linear(args.mlp_hidden_dim, args.mlp_hidden_dim)
        self.fc3 = nn.Linear(args.mlp_hidden_dim, 1)
        self.activate_func = [nn.Tanh(), nn.ReLU()][args.use_relu]
        if args.use_orthogonal_init:
            print("------use_orthogonal_init------")
            orthogonal_init(self.fc1)
            orthogonal_init(self.fc2)
            orthogonal_init(self.fc3)

    def forward(self, critic_input):
        x = self.activate_func(self.fc1(critic_input))
        x = self.activate_func(self.fc2(x))
        value = self.fc3(x)
        return value

# MAPPO MPE for Continuous Action Spaces

class MAPPO_MULTIWALKER:
    def __init__(self, args):
        self.N = args.N
        self.action_dim = args.action_dim
        self.obs_dim = args.obs_dim
        self.state_dim = args.state_dim
        self.episode_limit = args.max_cycles
        self.rnn_hidden_dim = args.rnn_hidden_dim

        self.batch_size = args.batch_size
        self.mini_batch_size = args.mini_batch_size
        self.max_train_steps = args.max_train_steps
        self.lr = args.lr
        self.gamma = args.gamma
        self.lamda = args.lamda
        self.epsilon = args.epsilon
        self.K_epochs = args.K_epochs
        self.entropy_coef = args.entropy_coef
        self.set_adam_eps = args.set_adam_eps
        self.use_grad_clip = args.use_grad_clip
        self.use_lr_decay = args.use_lr_decay
        self.use_adv_norm = args.use_adv_norm
        self.use_rnn = args.use_rnn
        self.add_agent_id = args.add_agent_id
        self.use_value_clip = args.use_value_clip

        # Define actor and critic input dimensions
        self.actor_input_dim = args.obs_dim
        self.critic_input_dim = args.state_dim
        if self.add_agent_id:
            print("------add agent id------")
            self.actor_input_dim += args.N
            self.critic_input_dim += args.N

        # Initialize networks based on RNN usage
        if self.use_rnn:
            print("------use rnn------")
            self.actor = Actor_RNN(args, self.actor_input_dim)
            self.critic = Critic_RNN(args, self.critic_input_dim)
        else:
            self.actor = Actor_MLP(args, self.actor_input_dim)
            self.critic = Critic_MLP(args, self.critic_input_dim)

        self.ac_parameters = list(self.actor.parameters()) + list(self.critic.parameters())
        if self.set_adam_eps:
            print("------set adam eps------")
            self.ac_optimizer = torch.optim.Adam(self.ac_parameters, lr=self.lr, eps=1e-5)
        else:
            self.ac_optimizer = torch.optim.Adam(self.ac_parameters, lr=self.lr)

    def choose_action(self, obs_n, evaluate):
        with torch.no_grad():
            keys = sorted(obs_n.keys())
            obs_list = [obs_n[key] for key in keys]
            obs_tensor = torch.tensor(np.stack(obs_list), dtype=torch.float32)

            actor_inputs = [obs_tensor]
            if self.add_agent_id:
                actor_inputs.append(torch.eye(self.N))
            actor_inputs = torch.cat(actor_inputs, dim=-1)

            if self.use_rnn:
                output = self.actor(actor_inputs)
                if isinstance(output, tuple):
                    mean, log_std = output
                else:
                    mean = output
                    log_std = torch.zeros_like(mean)
            else:
                mean, log_std = self.actor(actor_inputs)

            std = torch.exp(log_std)
            dist = Normal(mean, std)

            if evaluate:
                a_n = torch.tanh(mean)
                raw_a_n = mean
                a_logprob_n = dist.log_prob(mean).sum(dim=-1)
            else:
                raw_action = dist.sample()
                a_n = torch.tanh(raw_action)
                a_logprob_n = dist.log_prob(raw_action).sum(dim=-1)
                a_logprob_n -= torch.sum(2 * (np.log(2) - raw_action - F.softplus(-2 * raw_action)), dim=-1)
                raw_a_n = raw_action

            a_n_np = a_n.numpy()
            raw_a_n_np = raw_a_n.numpy()
            a_logprob_np = a_logprob_n.numpy()

            actions_dict = {key: a_n_np[i] for i, key in enumerate(keys)}
            raw_actions_dict = {key: raw_a_n_np[i] for i, key in enumerate(keys)}
            logprob_dict = {key: a_logprob_np[i] for i, key in enumerate(keys)}

            return raw_actions_dict, actions_dict, logprob_dict

    def get_value(self, s):
        with torch.no_grad():
            critic_inputs = []
            # Duplicate the global state for each agent
            s = torch.tensor(s, dtype=torch.float32).unsqueeze(0).repeat(self.N, 1)
            critic_inputs.append(s)
            if self.add_agent_id:
                critic_inputs.append(torch.eye(self.N))
            critic_inputs = torch.cat(critic_inputs, dim=-1)
            v_n = self.critic(critic_inputs)
            return v_n.numpy().flatten()

    def train(self, replay_buffer, total_steps):
        batch = replay_buffer.get_training_data()

        # Compute advantage using Generalized Advantage Estimation (GAE)
        adv = []
        gae = 0
        with torch.no_grad():
            deltas = batch['r_n'] + self.gamma * batch['v_n'][:, 1:] * (1 - batch['done_n']) - batch['v_n'][:, :-1]
            for t in reversed(range(self.episode_limit)):
                gae = deltas[:, t] + self.gamma * self.lamda * gae
                adv.insert(0, gae)
            adv = torch.stack(adv, dim=1)
            v_target = adv + batch['v_n'][:, :-1]
            if self.use_adv_norm:
                adv = ((adv - adv.mean()) / (adv.std() + 1e-5))

        actor_inputs, critic_inputs = self.get_inputs(batch)

        for _ in range(self.K_epochs):
            for index in BatchSampler(SequentialSampler(range(self.batch_size)), self.mini_batch_size, False):
                if self.use_rnn:
                    self.actor.rnn_hidden = None
                    self.critic.rnn_hidden = None
                    probs_now, values_now = [], []
                    for t in range(self.episode_limit):
                        mean, log_std = self.actor(actor_inputs[index, t].reshape(self.mini_batch_size * self.N, -1))
                        std = torch.exp(log_std)
                        dist_now = Normal(mean, std)
                        raw_a_n_t = batch['raw_a_n'][index, t].reshape(self.mini_batch_size * self.N, -1)
                        log_prob = dist_now.log_prob(raw_a_n_t).sum(dim=-1)
                        log_prob -= torch.sum(2 * (np.log(2) - raw_a_n_t - F.softplus(-2 * raw_a_n_t)), dim=-1)
                        probs_now.append(log_prob.reshape(self.mini_batch_size, self.N))
                        v = self.critic(critic_inputs[index, t].reshape(self.mini_batch_size * self.N, -1))
                        values_now.append(v.reshape(self.mini_batch_size, self.N))
                    a_logprob_n_now = torch.stack(probs_now, dim=1)
                    values_now = torch.stack(values_now, dim=1)
                else:
                    mean, log_std = self.actor(actor_inputs[index])
                    std = torch.exp(log_std)
                    dist_now = Normal(mean, std)
                    a_logprob_n_now = dist_now.log_prob(batch['raw_a_n'][index]).sum(dim=-1)
                    a_logprob_n_now -= torch.sum(2 * (np.log(2) - batch['raw_a_n'][index] - F.softplus(-2 * batch['raw_a_n'][index])), dim=-1)
                    values_now = self.critic(critic_inputs[index]).squeeze(-1)

                ratios = torch.exp(a_logprob_n_now - batch['a_logprob_n'][index].detach())
                surr1 = ratios * adv[index]
                surr2 = torch.clamp(ratios, 1 - self.epsilon, 1 + self.epsilon) * adv[index]
                actor_loss = -torch.min(surr1, surr2)

                # Compute entropy bonus
                entropy = dist_now.entropy().sum(dim=-1)

                if self.use_value_clip:
                    values_old = batch["v_n"][index, :-1].detach()
                    values_error_clip = torch.clamp(values_now - values_old, -self.epsilon, self.epsilon) + values_old - v_target[index]
                    values_error_original = values_now - v_target[index]
                    critic_loss = torch.max(values_error_clip ** 2, values_error_original ** 2)
                else:
                    critic_loss = (values_now - v_target[index]) ** 2

                ac_loss = actor_loss.mean() + critic_loss.mean() - self.entropy_coef * entropy.mean()

                self.ac_optimizer.zero_grad()
                ac_loss.backward()
                if self.use_grad_clip:
                    torch.nn.utils.clip_grad_norm_(self.ac_parameters, 10.0)
                self.ac_optimizer.step()

        if self.use_lr_decay:
            self.lr_decay(total_steps)

    def lr_decay(self, total_steps):
        lr_now = self.lr * (1 - total_steps / self.max_train_steps)
        for p in self.ac_optimizer.param_groups:
            p['lr'] = lr_now

    def get_inputs(self, batch):
        actor_inputs, critic_inputs = [], []
        actor_inputs.append(batch['obs_n'])
        critic_inputs.append(batch['s'].unsqueeze(2).repeat(1, 1, self.N, 1))
        if self.add_agent_id:
            agent_id_one_hot = torch.eye(self.N).unsqueeze(0).unsqueeze(0).repeat(self.batch_size, self.episode_limit, 1, 1)
            actor_inputs.append(agent_id_one_hot)
            critic_inputs.append(agent_id_one_hot)
        actor_inputs = torch.cat(actor_inputs, dim=-1)
        critic_inputs = torch.cat(critic_inputs, dim=-1)
        return actor_inputs, critic_inputs

    def save_model(self, env_name, number, seed, total_steps):
        torch.save(self.actor.state_dict(), 
                   "./model/MAPPO_actor_env_{}_number_{}_seed_{}_step_{}k.pth".format(
                       env_name, number, seed, int(total_steps / 1000)))

    def load_model(self, env_name, number, seed, step):
        self.actor.load_state_dict(torch.load(
            "./model/MAPPO_actor_env_{}_number_{}_seed_{}_step_{}k.pth".format(
                env_name, number, seed, step)))
