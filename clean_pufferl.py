from dataclasses import field
from functools import partial
import heapq
import json
import math
from multiprocessing import Queue
from pdb import set_trace as T
import numpy as np
import contextlib
import ast

import os
import random
import time

from collections import defaultdict, deque

import torch

import pufferlib
import pufferlib.utils
import pufferlib.pytorch

torch.set_float32_matmul_precision('high')

# Fast Cython GAE implementation
import pyximport
pyximport.install(setup_args={"include_dirs": np.get_include()})
from c_gae import compute_gae


def optimize(data, config, loss):
    #data.optimizer.zero_grad()
    loss.backward()
    torch.nn.utils.clip_grad_norm_(data.policy.parameters(), config.max_grad_norm)
    data.optimizer.step()
    if config.device == 'cuda':
        torch.cuda.synchronize()


def create(config, vecenv, policy, async_config, optimizer=None, wandb=None):
    seed_everything(config.seed, config.torch_deterministic)
    profile = Profile()
    losses = make_losses()

    msg = f'Model Size: {abbreviate(count_params(policy))} parameters'
    # TODO: Check starting point in term and draw from there with no clear
    print_dashboard(config.env, 0, 0, profile, losses, {}, msg, clear=True)

    vecenv.async_reset(config.seed)
    obs_shape = vecenv.single_observation_space.shape
    obs_dtype = vecenv.single_observation_space.dtype
    atn_shape = vecenv.single_action_space.shape
    total_agents = vecenv.num_agents

    lstm = policy.lstm if hasattr(policy, 'lstm') else None
    experience = Experience(config.batch_size, vecenv.agents_per_batch, config.bptt_horizon,
        config.minibatch_size, obs_shape, obs_dtype, atn_shape, config.cpu_offload, config.device, lstm, total_agents)

    uncompiled_policy = policy

    if config.compile:
        policy = torch.compile(policy, mode=config.compile_mode)
    # breakpoint()
    optimizer = torch.optim.Adam(policy.parameters(),
        lr=config.learning_rate, eps=1e-5)
    
    env_send_queues = async_config['send_queues']
    env_recv_queues = async_config['recv_queues']
    states: dict = field(default_factory=lambda: defaultdict(partial(deque, maxlen=1)))
    event_tracker: dict = field(default_factory=lambda: {})
    max_event_count: int = 0

    return pufferlib.namespace(
        config=config,
        vecenv=vecenv,
        policy=policy,
        uncompiled_policy=uncompiled_policy,
        optimizer=optimizer,
        experience=experience,
        profile=profile,
        losses=losses,
        wandb=wandb,
        global_step=0,
        epoch=0,
        stats={},
        msg=msg,
        last_log_time=time.time(),
        env_send_queues=env_send_queues,
        env_recv_queues=env_recv_queues,
        states=states,
        event_tracker=event_tracker,
        max_event_count=max_event_count,
    )

@pufferlib.utils.profile
def evaluate(data):
    config, profile, experience = data.config, data.profile, data.experience

    with profile.eval_misc:
        policy = data.policy
        infos = defaultdict(list)
        lstm_h, lstm_c = experience.lstm_h, experience.lstm_c


    while not experience.full:
        with profile.env:
            o, r, d, t, info, env_id, mask = data.vecenv.recv()
            env_id = env_id.tolist()

        with profile.eval_misc:
            data.global_step += sum(mask)

            o = torch.as_tensor(o)
            o_device = o.to(config.device)
            r = torch.as_tensor(r)
            d = torch.as_tensor(d)

        with profile.eval_forward, torch.no_grad():
            # TODO: In place-update should be faster. Leaking 7% speed max
            # Also should be using a cuda tensor to index
            if lstm_h is not None:
                h = lstm_h[:, env_id]
                c = lstm_c[:, env_id]
                actions, logprob, _, value, (h, c) = policy(o_device, (h, c))
                lstm_h[:, env_id] = h
                lstm_c[:, env_id] = c
            else:
                actions, logprob, _, value = policy(o_device)

            if config.device == 'cuda':
                torch.cuda.synchronize()

        with profile.eval_misc:
            value = value.flatten()
            actions = actions.cpu().numpy()
            mask = torch.as_tensor(mask)# * policy.mask)
            o = o if config.cpu_offload else o_device
            experience.store(o, value, actions, logprob, r, d, env_id, mask)

            for i in info:
                for k, v in pufferlib.utils.unroll_nested_dict(i):
                    if "state/" in k:
                        _, key = k.split("/")
                        key: tuple[str] = ast.literal_eval(key)
                        data.states[key].append(v)
                    # elif "required_count" == k:
                    #     for count, eid in zip(
                    #         infos["required_count"], infos["env_id"]
                    #     ):
                    #         data.event_tracker[eid] = count
                    #     infos[k].append(v)
                    else:
                        infos[k].append(v)
                    infos[k].append(v)

        with profile.env:
            data.vecenv.send(actions)

    with profile.eval_misc:
        if (hasattr(data.config, "swarm_keep_pct") and "events" in infos and "state" in infos):
            largest = [x[0] for x in heapq.nlargest(math.ceil(data.config.num_envs * data.config.swarm_keep_pct), enumerate(infos["events"]), key=lambda x: x[1],)]
            reset_states = [random.choice(largest) if i not in largest else i for i in range(data.config.num_envs)]
            print(f"Migrating states: {','.join(str(i) + '->' + str(n) for i, n in enumerate(reset_states))}")
            print(f"Migrating states: {','.join(str(i) + '->' + str(n) for i, n in enumerate(reset_states))}")
            print(f"Migrating states: {','.join(str(i) + '->' + str(n) for i, n in enumerate(reset_states))}")
            print(f"Migrating states: {','.join(str(i) + '->' + str(n) for i, n in enumerate(reset_states))}")
            print(f"Migrating states: {','.join(str(i) + '->' + str(n) for i, n in enumerate(reset_states))}")
            for i in range(data.config.num_envs):
                data.env_recv_queues[i].put(infos["state"][reset_states[i]])
            for i in range(data.config.num_envs):
                data.env_send_queues[i].get()

        data.stats = {}

        # Moves into models... maybe. Definitely moves.
        # You could also just return infos and have it in demo
        if 'pokemon_exploration_map' in infos:
            for pmap in infos['pokemon_exploration_map']:
                if not hasattr(data, 'pokemon_map'):
                    import pokemon_red_eval
                    data.map_updater = pokemon_red_eval.map_updater()
                    data.pokemon_map = pmap

                data.pokemon_map = np.maximum(data.pokemon_map, pmap)

            if len(infos['pokemon_exploration_map']) > 0:
                rendered = data.map_updater(data.pokemon_map)
                data.stats['Media/exploration_map'] = data.wandb.Image(rendered)

        for k, v in infos.items():
            if '_map' in k and data.wandb is not None:
                data.stats[f'Media/{k}'] = data.wandb.Image(v[0])
                continue

            try: # TODO: Better checks on log data types
                data.stats[k] = np.mean(v)
            except:
                continue


    return data.stats, infos

def compute_advantages(batch_size, idxs, dones, values, rewards, gamma, gae_lambda):
    advantages = np.zeros(batch_size)
    lastgaelam = 0
    for t in reversed(range(batch_size-1)):
        i, i_nxt = idxs[t], idxs[t + 1]

        nextnonterminal = 1.0 - dones[i_nxt]
        nextvalues = values[i_nxt]
        delta = (
            rewards[i_nxt]
            + gamma * nextvalues * nextnonterminal
            - values[i]
        )
        advantages[t] = lastgaelam = (
            delta + gamma * gae_lambda * nextnonterminal * lastgaelam
        )

    return advantages

@pufferlib.utils.profile
def train(data):
    config, profile, experience = data.config, data.profile, data.experience
    data.losses = make_losses()
    losses = data.losses

    with profile.train_misc:
        idxs = experience.sort_training_data()
        dones_np = experience.dones_np[idxs]
        values_np = experience.values_np[idxs]
        rewards_np = experience.rewards_np[idxs]
        # TODO: bootstrap between segment bounds
        advantages_np = compute_gae(dones_np, values_np,
            rewards_np, config.gamma, config.gae_lambda)
        experience.flatten_batch(advantages_np)

    # Optimizing the policy and value network
    mean_pg_loss, mean_v_loss, mean_entropy_loss = 0, 0, 0
    mean_old_kl, mean_kl, mean_clipfrac = 0, 0, 0
    for epoch in range(config.update_epochs):
        lstm_state = None
        for mb in range(experience.num_minibatches):
            with profile.train_misc:
                obs = experience.b_obs[mb]
                obs = obs.to(config.device)
                atn = experience.b_actions[mb]
                log_probs = experience.b_logprobs[mb]
                val = experience.b_values[mb]
                adv = experience.b_advantages[mb]
                ret = experience.b_returns[mb]

            with profile.train_forward:
                if experience.lstm_h is not None:
                    _, newlogprob, entropy, newvalue, lstm_state = data.policy(
                        obs, state=lstm_state, action=atn)
                    lstm_state = (lstm_state[0].detach(), lstm_state[1].detach())
                else:
                    _, newlogprob, entropy, newvalue = data.policy(
                        obs.reshape(-1, *data.vecenv.single_observation_space.shape),
                        action=atn,
                    )

                if config.device == 'cuda':
                    torch.cuda.synchronize()

            with profile.train_misc:
                logratio = newlogprob - log_probs.reshape(-1)
                ratio = logratio.exp()

                with torch.no_grad():
                    # calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratio - 1) - logratio).mean()
                    clipfrac = ((ratio - 1.0).abs() > config.clip_coef).float().mean()

                adv = adv.reshape(-1)
                if config.norm_adv:
                    adv = (adv - adv.mean()) / (adv.std() + 1e-8)

                # Policy loss
                pg_loss1 = -adv * ratio
                pg_loss2 = -adv * torch.clamp(
                    ratio, 1 - config.clip_coef, 1 + config.clip_coef
                )
                pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                # Value loss
                newvalue = newvalue.view(-1)
                if config.clip_vloss:
                    v_loss_unclipped = (newvalue - ret) ** 2
                    v_clipped = val + torch.clamp(
                        newvalue - val,
                        -config.vf_clip_coef,
                        config.vf_clip_coef,
                    )
                    v_loss_clipped = (v_clipped - ret) ** 2
                    v_loss_max = torch.max(v_loss_unclipped, v_loss_clipped)
                    v_loss = 0.5 * v_loss_max.mean()
                else:
                    v_loss = 0.5 * ((newvalue - ret) ** 2).mean()

                entropy_loss = entropy.mean()
                loss = pg_loss - config.ent_coef * entropy_loss + v_loss * config.vf_coef

            with profile.learn:
                data.optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(data.policy.parameters(), config.max_grad_norm)
                data.optimizer.step()
                if config.device == 'cuda':
                    torch.cuda.synchronize()
                #data.optim(data, config, loss)

            with profile.train_misc:
                losses.policy_loss += pg_loss.item() / experience.num_minibatches
                losses.value_loss += v_loss.item() / experience.num_minibatches
                losses.entropy += entropy_loss.item() / experience.num_minibatches
                losses.old_approx_kl += old_approx_kl.item() / experience.num_minibatches
                losses.approx_kl += approx_kl.item() / experience.num_minibatches
                losses.clipfrac += clipfrac.item() / experience.num_minibatches

        if config.target_kl is not None:
            if approx_kl > config.target_kl:
                break

    with profile.train_misc:
        if config.anneal_lr:
            frac = 1.0 - data.global_step / config.total_timesteps
            lrnow = frac * config.learning_rate
            data.optimizer.param_groups[0]["lr"] = lrnow

        y_pred = experience.values_np
        y_true = experience.returns_np
        var_y = np.var(y_true)
        explained_var = np.nan if var_y == 0 else 1 - np.var(y_true - y_pred) / var_y
        losses.explained_variance = explained_var
        data.epoch += 1

        done_training = data.global_step >= config.total_timesteps
        if profile.update(data) or done_training:
            print_dashboard(config.env, data.global_step, data.epoch,
                profile, data.losses, data.stats, data.msg)

            if data.wandb is not None and data.global_step > 0 and time.time() - data.last_log_time > 5.0:
                data.last_log_time = time.time()
                data.wandb.log({
                    '0verview/SPS': profile.SPS,
                    '0verview/agent_steps': data.global_step,
                    '0verview/learning_rate': data.optimizer.param_groups[0]["lr"],
                    **{f'environment/{k}': v for k, v in data.stats.items()},
                    **{f'losses/{k}': v for k, v in data.losses.items()},
                    **{f'performance/{k}': v for k, v in data.profile},
                })
                columns, embeddings = data.policy.policy.get_embeds()
                embedding_data = {"pokemon": [columns], "coords": [embeddings]}
                with open('pokemon_data.json', 'w') as f:
                    json.dump(embedding_data, f, indent=4)

        if data.epoch % config.checkpoint_interval == 0 or done_training:
            save_checkpoint(data)
            # columns, embeddings = data.policy.policy.get_embeds()
            # data.wandb.log({'embeddings': data.wandb.Table(columns=columns, data=embeddings)})
            # data.msg = f'Checkpoint saved at update {data.epoch}'

        if done_training:
            close(data)

def close(data):
    data.vecenv.close()
    config = data.config
    if data.wandb is not None:
        artifact_name = f"{config.exp_id}_model"
        artifact = data.wandb.Artifact(artifact_name, type="model")
        model_path = save_checkpoint(data)
        artifact.add_file(model_path)
        data.wandb.run.log_artifact(artifact)
        data.wandb.finish()

class Profile:
    SPS: ... = 0
    uptime: ... = 0
    remaining: ... = 0
    eval_time: ... = 0
    env_time: ... = 0
    eval_forward_time: ... = 0
    eval_misc_time: ... = 0
    train_time: ... = 0
    train_forward_time: ... = 0
    learn_time: ... = 0
    train_misc_time: ... = 0
    def __init__(self):
        self.start = time.time()
        self.env = pufferlib.utils.Profiler()
        self.eval_forward = pufferlib.utils.Profiler()
        self.eval_misc = pufferlib.utils.Profiler()
        self.train_forward = pufferlib.utils.Profiler()
        self.learn = pufferlib.utils.Profiler()
        self.train_misc = pufferlib.utils.Profiler()
        self.prev_steps = 0

    def __iter__(self):
        yield 'SPS', self.SPS
        yield 'uptime', self.uptime
        yield 'remaining', self.remaining
        yield 'eval_time', self.eval_time
        yield 'env_time', self.env_time
        yield 'eval_forward_time', self.eval_forward_time
        yield 'eval_misc_time', self.eval_misc_time
        yield 'train_time', self.train_time
        yield 'train_forward_time', self.train_forward_time
        yield 'learn_time', self.learn_time
        yield 'train_misc_time', self.train_misc_time

    @property
    def epoch_time(self):
        return self.train_time + self.eval_time

    def update(self, data, interval_s=1):
        global_step = data.global_step
        if global_step == 0:
            return True

        uptime = time.time() - self.start
        if uptime - self.uptime < interval_s:
            return False

        self.SPS = (global_step - self.prev_steps) / (uptime - self.uptime)
        self.prev_steps = global_step
        self.uptime = uptime

        self.remaining = (data.config.total_timesteps - global_step) / self.SPS
        self.eval_time = data._timers['evaluate'].elapsed
        self.eval_forward_time = self.eval_forward.elapsed
        self.env_time = self.env.elapsed
        self.eval_misc_time = self.eval_misc.elapsed
        self.train_time = data._timers['train'].elapsed
        self.train_forward_time = self.train_forward.elapsed
        self.learn_time = self.learn.elapsed
        self.train_misc_time = self.train_misc.elapsed
        return True

def make_losses():
    return pufferlib.namespace(
        policy_loss=0,
        value_loss=0,
        entropy=0,
        old_approx_kl=0,
        approx_kl=0,
        clipfrac=0,
        explained_variance=0,
    )

class Experience:
    '''Flat tensor storage and array views for faster indexing'''
    def __init__(self, batch_size, agents_per_batch, bptt_horizon, minibatch_size, obs_shape, obs_dtype, atn_shape,
                 cpu_offload=False, device='cuda', lstm=None, lstm_total_agents=0):
        obs_dtype = pufferlib.pytorch.numpy_to_torch_dtype_dict[obs_dtype]
        pin = device == 'cuda' and cpu_offload
        obs_device = device if not pin else 'cpu'
        self.obs=torch.zeros(batch_size, *obs_shape, dtype=obs_dtype,
            pin_memory=pin, device=device if not pin else 'cpu')
        self.actions=torch.zeros(batch_size, *atn_shape, dtype=int, pin_memory=pin)
        self.logprobs=torch.zeros(batch_size, pin_memory=pin)
        self.rewards=torch.zeros(batch_size, pin_memory=pin)
        self.dones=torch.zeros(batch_size, pin_memory=pin)
        self.truncateds=torch.zeros(batch_size, pin_memory=pin)
        self.values=torch.zeros(batch_size, pin_memory=pin)

        #self.obs_np = np.asarray(self.obs)
        self.actions_np = np.asarray(self.actions)
        self.logprobs_np = np.asarray(self.logprobs)
        self.rewards_np = np.asarray(self.rewards)
        self.dones_np = np.asarray(self.dones)
        self.truncateds_np = np.asarray(self.truncateds)
        self.values_np = np.asarray(self.values)

        self.lstm_h = self.lstm_c = None
        if lstm is not None:
            assert lstm_total_agents > 0
            shape = (lstm.num_layers, lstm_total_agents, lstm.hidden_size)
            self.lstm_h = torch.zeros(shape).to(device)
            self.lstm_c = torch.zeros(shape).to(device)

        num_minibatches = batch_size / minibatch_size
        self.num_minibatches = int(num_minibatches)
        if self.num_minibatches != num_minibatches:
            raise ValueError('batch_size must be divisible by minibatch_size')

        minibatch_rows = minibatch_size / bptt_horizon
        self.minibatch_rows = int(minibatch_rows)
        if self.minibatch_rows != minibatch_rows:
            raise ValueError('minibatch_size must be divisible by bptt_horizon')

        self.batch_size = batch_size
        self.bptt_horizon = bptt_horizon
        self.minibatch_size = minibatch_size
        self.device = device
        self.sort_keys = []
        self.ptr = 0
        self.step = 0

    @property
    def full(self):
        return self.ptr >= self.batch_size

    def store(self, obs, value, action, logprob, reward, done, env_id, mask):
        # Mask learner and Ensure indices do not exceed batch size
        ptr = self.ptr
        indices = torch.where(mask)[0].numpy()[:self.batch_size - ptr]
        end = ptr + len(indices)
 
        self.obs[ptr:end] = obs.to(self.obs.device)[indices]
        self.values_np[ptr:end] = value.cpu().numpy()[indices]
        self.actions_np[ptr:end] = action[indices]
        self.logprobs_np[ptr:end] = logprob.cpu().numpy()[indices]
        self.rewards_np[ptr:end] = reward.cpu().numpy()[indices]
        self.dones_np[ptr:end] = done.cpu().numpy()[indices]
        self.sort_keys.extend([(env_id[i], self.step) for i in indices])
        self.ptr = end
        self.step += 1

    def sort_training_data(self):
        idxs = np.asarray(sorted(
            range(len(self.sort_keys)), key=self.sort_keys.__getitem__))
        self.b_idxs_obs = torch.as_tensor(idxs.reshape(
                self.minibatch_rows, self.num_minibatches, self.bptt_horizon
            ).transpose(1,0,-1)).to(self.obs.device).long()
        self.b_idxs = self.b_idxs_obs.to(self.device)
        self.b_idxs_flat = self.b_idxs.reshape(
            self.num_minibatches, self.minibatch_size)
        self.sort_keys = []
        self.ptr = 0
        self.step = 0
        return idxs

    def flatten_batch(self, advantages_np):
        advantages = torch.from_numpy(advantages_np).to(self.device)
        b_idxs, b_flat = self.b_idxs, self.b_idxs_flat
        self.b_actions = self.actions.to(self.device, non_blocking=True)
        self.b_logprobs = self.logprobs.to(self.device, non_blocking=True)
        self.b_dones = self.dones.to(self.device, non_blocking=True)
        self.b_values = self.values.to(self.device, non_blocking=True)
        self.b_advantages = advantages.reshape(self.minibatch_rows,
            self.num_minibatches, self.bptt_horizon).transpose(0, 1).reshape(
            self.num_minibatches, self.minibatch_size)
        self.returns_np = advantages_np + self.values_np
        self.b_obs = self.obs[self.b_idxs_obs]
        self.b_actions = self.b_actions[b_idxs].contiguous()
        self.b_logprobs = self.b_logprobs[b_idxs]
        self.b_dones = self.b_dones[b_idxs]
        self.b_values = self.b_values[b_flat]
        self.b_returns = self.b_advantages + self.b_values

def save_checkpoint(data):
    config = data.config
    path = os.path.join(config.data_dir, config.exp_id)
    if not os.path.exists(path):
        os.makedirs(path)

    model_name = f'model_{data.epoch:06d}.pt'
    model_path = os.path.join(path, model_name)
    if os.path.exists(model_path):
        return model_path

    torch.save(data.uncompiled_policy, model_path)

    state = {
        'optimizer_state_dict': data.optimizer.state_dict(),
        'global_step': data.global_step,
        'agent_step': data.global_step,
        'update': data.epoch,
        'model_name': model_name,
        'exp_id': config.exp_id,
    }
    state_path = os.path.join(path, 'trainer_state.pt')
    torch.save(state, state_path + '.tmp')
    os.rename(state_path + '.tmp', state_path)

    return model_path

def try_load_checkpoint(data):
    config = data.config
    path = os.path.join(config.data_dir, config.exp_id)
    if not os.path.exists(path):
        print('No checkpoints found. Assuming new experiment')
        return

    trainer_path = os.path.join(path, 'trainer_state.pt')
    resume_state = torch.load(trainer_path)
    model_path = os.path.join(path, resume_state['model_name'])
    data.policy.uncompiled.load_state_dict(model_path, map_location=config.device)
    data.optimizer.load_state_dict(resume_state['optimizer_state_dict'])
    print(f'Loaded checkpoint {resume_state["model_name"]}')

def count_params(policy):
    return sum(p.numel() for p in policy.parameters() if p.requires_grad)

def rollout(env_creator, env_kwargs, agent_creator, agent_kwargs,
        model_path=None, device='cuda', verbose=True):
    os.system('clear')
    try:
        env = env_creator(render_mode='rgb_array', **env_kwargs)
    except:
        env = env_creator(**env_kwargs)

    if model_path is None:
        agent = agent_creator(env, **agent_kwargs)
    else:
        agent = torch.load(model_path, map_location=device)

    terminal = truncated = True
    while True:
        if terminal or truncated:
            ob, info = env.reset()
            state = None
            step = 0
            reward = 0
            terminal = False
            truncated = False
            return_val = 0
        else:
            ob, reward, terminal, truncated, _ = env.step(action.item())

        ob = torch.tensor(ob).unsqueeze(0).to(device)
        with torch.no_grad():
            if hasattr(agent, 'lstm'):
                action, _, _, _, state = agent(ob, state)
            else:
                action, _, _, _ = agent(ob)

        return_val += reward

        render = env.render()
        if env.render_mode == 'ansi':
            print('\033[0;0H' + render + '\n')
            time.sleep(0.6)
        elif env.render_mode == 'rgb_array':
            import cv2
            render = cv2.cvtColor(render, cv2.COLOR_RGB2BGR)
            cv2.imshow('frame', render)
            cv2.waitKey(1)
            time.sleep(1/24)

        if verbose:
            print(f'Step: {step} Reward: {reward:.4f} Return: {return_val:.2f}')

        step += 1

def seed_everything(seed, torch_deterministic):
    random.seed(seed)
    np.random.seed(seed)
    if seed is not None:
        torch.manual_seed(seed)
    torch.backends.cudnn.deterministic = torch_deterministic

import psutil
import GPUtil

import rich
from rich.console import Console
from rich.table import Table

ROUND_OPEN = rich.box.Box(
    "╭──╮\n"
    "│  │\n"
    "│  │\n"
    "│  │\n"
    "│  │\n"
    "│  │\n"
    "│  │\n"
    "╰──╯\n"
)

c1 = '[bright_cyan]'
c2 = '[white]'
c3 = '[cyan]'
b1 = '[bright_cyan]'
b2 = '[bright_white]'

def abbreviate(num):
    if num < 1e3:
        return f'{b2}{num:.0f}'
    elif num < 1e6:
        return f'{b2}{num/1e3:.1f}{c2}k'
    elif num < 1e9:
        return f'{b2}{num/1e6:.1f}{c2}m'
    elif num < 1e12:
        return f'{b2}{num/1e9:.1f}{c2}b'
    else:
        return f'{b2}{num/1e12:.1f}{c2}t'

def duration(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{b2}{h}{c2}h {b2}{m}{c2}m {b2}{s}{c2}s" if h else f"{b2}{m}{c2}m {b2}{s}{c2}s" if m else f"{b2}{s}{c2}s"


def fmt_perf(name, time, uptime):
    percent = 0 if uptime == 0 else int(100*time/uptime - 1e-5)
    return f'{c1}{name}', duration(time), f'{b2}{percent:2d}%'

# TODO: Add env name to print_dashboard
def print_dashboard(env_name, global_step, epoch, profile, losses, stats, msg, clear=False, max_stats=[0]):
    console = Console()
    if clear:
        console.clear()

    dashboard = Table(box=ROUND_OPEN, expand=True,
        show_header=False, border_style='bright_cyan')

    table = Table(box=None, expand=True, show_header=False)
    dashboard.add_row(table)
    cpu_percent = psutil.cpu_percent()
    dram_percent = psutil.virtual_memory().percent
    gpus = GPUtil.getGPUs()
    gpu_percent = gpus[0].load * 100 if gpus else 0
    vram_percent = gpus[0].memoryUtil * 100 if gpus else 0
    table.add_column(justify="left", width=30)
    table.add_column(justify="center", width=12)
    table.add_column(justify="center", width=12)
    table.add_column(justify="center", width=12)
    table.add_column(justify="right", width=12)
    table.add_row(
        f':blowfish: {c1}PufferLib {b2}1.0.0{c1}: {env_name}',
        f'{c1}CPU: {c3}{cpu_percent:.1f}%',
        f'{c1}GPU: {c3}{gpu_percent:.1f}%',
        f'{c1}DRAM: {c3}{dram_percent:.1f}%',
        f'{c1}VRAM: {c3}{vram_percent:.1f}%',
    )
        
    s = Table(box=None, expand=True)
    s.add_column(f"{c1}Summary", justify='left', vertical='top', width=16)
    s.add_column(f"{c1}Value", justify='right', vertical='top', width=8)
    s.add_row(f'{c2}Agent Steps', abbreviate(global_step))
    s.add_row(f'{c2}SPS', abbreviate(profile.SPS))
    s.add_row(f'{c2}Epoch', abbreviate(epoch))
    s.add_row(f'{c2}Uptime', duration(profile.uptime))
    s.add_row(f'{c2}Remaining', duration(profile.remaining))

    p = Table(box=None, expand=True, show_header=False)
    p.add_column(f"{c1}Performance", justify="left", width=10)
    p.add_column(f"{c1}Time", justify="right", width=8)
    p.add_column(f"{c1}%", justify="right", width=4)
    p.add_row(*fmt_perf('Evaluate', profile.eval_time, profile.uptime))
    p.add_row(*fmt_perf('  Forward', profile.eval_forward_time, profile.uptime))
    p.add_row(*fmt_perf('  Env', profile.env_time, profile.uptime))
    p.add_row(*fmt_perf('  Misc', profile.eval_misc_time, profile.uptime))
    p.add_row(*fmt_perf('Train', profile.train_time, profile.uptime))
    p.add_row(*fmt_perf('  Forward', profile.train_forward_time, profile.uptime))
    p.add_row(*fmt_perf('  Learn', profile.learn_time, profile.uptime))
    p.add_row(*fmt_perf('  Misc', profile.train_misc_time, profile.uptime))

    l = Table(box=None, expand=True, )
    l.add_column(f'{c1}Losses', justify="left", width=16)
    l.add_column(f'{c1}Value', justify="right", width=8)
    for metric, value in losses.items():
        l.add_row(f'{c2}{metric}', f'{b2}{value:.3f}')

    monitor = Table(box=None, expand=True, pad_edge=False)
    monitor.add_row(s, p, l)
    dashboard.add_row(monitor)

    table = Table(box=None, expand=True, pad_edge=False)
    dashboard.add_row(table)
    left = Table(box=None, expand=True)
    right = Table(box=None, expand=True)
    table.add_row(left, right)
    left.add_column(f"{c1}User Stats", justify="left", width=20)
    left.add_column(f"{c1}Value", justify="right", width=10)
    right.add_column(f"{c1}User Stats", justify="left", width=20)
    right.add_column(f"{c1}Value", justify="right", width=10)
    i = 0
    for metric, value in stats.items():
        try: # Discard non-numeric values
            int(value)
        except:
            continue

        u = left if i % 2 == 0 else right
        u.add_row(f'{c2}{metric}', f'{b2}{value:.3f}')
        i += 1

    for i in range(max_stats[0] - i):
        u = left if i % 2 == 0 else right
        u.add_row('', '')

    max_stats[0] = max(max_stats[0], i)

    table = Table(box=None, expand=True, pad_edge=False)
    dashboard.add_row(table)
    table.add_row(f' {c1}Message: {c2}{msg}')

    with console.capture() as capture:
        console.print(dashboard)

    print('\033[0;0H' + capture.get())
