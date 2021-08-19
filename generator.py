import os
from collections import defaultdict
from preprocessing import MinigridPreprocess
from models import Dreamer
from typing import Tuple, Optional, Dict, List
import argparse
import numpy as np
import scipy.signal
import datetime
import time
import pathlib
import collections
from numba import njit
import gym
from envs import MiniGrid, Atari
import mlflow

from tools import *

WALL = 2


def main(env_id='MiniGrid-MazeS11N-v0',
         seed=0,
         policy='random',
         num_steps=int(1e6),
         env_max_steps=500,
         steps_per_npz=2_000,
         model_reload_interval=60,
         model_conf=dict(),
         log_mlflow_metrics=True,
         ):

    # Mlflow

    if 'MLFLOW_RUN_ID' in os.environ:
        run = mlflow.active_run()
        if run is None:
            run = mlflow.start_run()
        print(f'Generator using existing mlflow run {run.info.run_id}')
    else:
        run = mlflow.start_run(run_name=f'{env_id}-s{seed}')
        print(f'Mlflow run {run.info.run_id} in experiment {run.info.experiment_id}')

    artifact_dir = run.info.artifact_uri.replace('file://', '') + '/episodes'
    if artifact_dir.startswith('gs:/') or artifact_dir.startswith('s3:/'):
        artifact_dir = Pathy(artifact_dir)
    else:
        artifact_dir = Path(artifact_dir)

    # Env

    if env_id.startswith('MiniGrid-'):
        env = MiniGrid(env_id, max_steps=env_max_steps, seed=seed)

    elif env_id.startswith('Atari-'):
        env = Atari(env_id.split('-')[1].lower())
        # TODO: max_steps wrapper

    elif env_id.startswith('AtariGray-'):
        env = Atari(env_id.split('-')[1].lower(), grayscale=True)

    elif env_id.startswith('MiniWorld-'):
        import gym_miniworld.wrappers as wrap
        env = env_raw = gym.make(env_id, max_steps=env_max_steps)
        env = wrap.DictWrapper(env)
        env = wrap.MapWrapper(env)
        env = wrap.PixelMapWrapper(env)
        env = wrap.AgentPosWrapper(env)

    else:
        env = gym.make(env_id, max_steps=env_max_steps)

    env = ActionRewardResetWrapper(env, env_max_steps)
    env = CollectWrapper(env)

    # Policy

    model = None
    if policy == 'network':
        conf = model_conf
        model = Dreamer(conf)
        preprocess = MinigridPreprocess(image_categorical=conf.image_channels if conf.image_categorical else None,
                                        image_key=conf.image_key,
                                        map_categorical=conf.map_channels if conf.map_categorical else None,
                                        map_key=conf.map_key)
        policy = NetworkPolicy(model, preprocess)

    elif policy == 'random':
        policy = RandomPolicy(env.action_space)
    elif policy == 'minigrid_wander':
        policy = MinigridWanderPolicy()
    elif policy == 'maze_bouncing_ball':
        policy = MazeBouncingBallPolicy()
    elif policy == 'maze_dijkstra':
        step_size = env_raw.params.params['forward_step'].default / env_raw.room_size  # type: ignore
        turn_size = env_raw.params.params['turn_step'].default  # type: ignore
        policy = MazeDijkstraPolicy(step_size, turn_size)
    else:
        assert False, 'Unknown policy'

    # RUN

    steps, episodes = count_steps(artifact_dir)
    datas = []
    visited_stats = []
    first_save = True
    first_episode = True
    last_model_load = 0
    model_step = 0

    while steps < num_steps:

        if model is not None:
            if time.time() - last_model_load > model_reload_interval:
                while True:
                    model_step = mlflow_load_checkpoint(policy.model)  # type: ignore
                    if model_step:
                        print(f'Generator loaded model checkpoint {model_step}')
                        last_model_load = time.time()
                        break
                    else:
                        print('Generator model checkpoint not found, waiting...')
                        time.sleep(10)

        # Unroll one episode

        epsteps = 0
        timer = time.time()
        obs = env.reset()
        done = False
        metrics = defaultdict(list)

        while not done:
            action, mets = policy(obs)
            obs, reward, done, info = env.step(action)
            steps += 1
            epsteps += 1
            for k, v in mets.items():
                metrics[k].append(v)

        episodes += 1
        data = info['episode']  # type: ignore
        if 'policy_value' in metrics:
            data['policy_value'] = np.array(metrics['policy_value'] + [np.nan])     # last terminal value is null
            data['policy_entropy'] = np.array(metrics['policy_entropy'] + [np.nan]) # last policy is null
            data['action_prob'] = np.array([np.nan] + metrics['action_prob'])       # first action is null

        # Calculate visited (for MiniGrid/MiniWorld)

        if 'agent_pos' in data:
            agent_pos = data['agent_pos']
            agent_pos = np.floor(agent_pos / 2)
            agent_pos_visited = len(np.unique(agent_pos, axis=0))
            visited_pct = agent_pos_visited / 25
            visited_stats.append(visited_pct)
        else:
            visited_pct = np.nan

        # Log

        fps = epsteps / (time.time() - timer + 1e-6)
        if first_episode:
            print('Episode data sample: ', {k: v.shape for k, v in data.items()})
            first_episode = False

        print(f"Episode recorded:"
              f"  steps: {epsteps}"
              f",  reward: {data['reward'].sum()}"
              #   f",  explored%: {visited_pct:.1%}|{np.mean(visited_stats):.1%}"
              f",  fps: {fps:.0f}"
              f",  total steps: {steps:.0f}"
              f",  episodes: {episodes}"
              )

        def discount(x: np.ndarray, gamma: float = 0.999):
            return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

        if log_mlflow_metrics:
            log_step = model_step if model else steps
            metrics = {f'agent/{k}': np.mean(v) for k, v in metrics.items()}
            metrics.update({
                'agent/episode_length': epsteps,
                'agent/fps': fps,
                'agent/steps': steps,
                'agent/episodes': episodes,
                'agent/return': data['reward'].sum(),
                'agent/return_discounted': discount(data['reward']).mean(),
            })  # type: ignore
            mlflow.log_metrics(metrics, step=log_step)

        # Save to npz

        datas.append(data)
        datas_episodes = len(datas)
        datas_steps = sum(len(d['reset']) - 1 for d in datas)

        if datas_steps >= steps_per_npz:

            # Concatenate episodes

            data = {}
            for key in datas[0]:
                data[key] = np.concatenate([b[key] for b in datas], axis=0)
            datas = []

            # NHWC => HWCN for better compression

            data['image_t'] = data['image'].transpose(1, 2, 3, 0)
            del data['image']

            # Save to npz

            if first_save:
                print('Saved data sample: ', {k: v.shape for k, v in data.items()})
                first_save = False

            if datas_episodes > 1:
                fname = f's{seed}-ep{episodes-datas_episodes:06}_{episodes-1:06}-{datas_steps:04}.npz'
            else:
                fname = f's{seed}-ep{episodes-1:06}-{datas_steps:04}.npz'

            mlflow_log_npz(data, fname, 'episodes', verbose=True)

    print(f'Generator {seed} done.')


def count_steps(artifact_dir):
    files = list(sorted(artifact_dir.glob('*.npz')))
    steps = 0
    episodes = 0
    for f in files:
        # Example: f.name == '.../s1-ep0000_0003-1500.npz'
        sstep = f.name.split('.')[0].split('-')[-1]
        if sstep.isnumeric():
            steps += int(sstep)
        sepisode = f.name.split('.')[0].split('-')[-2].replace('ep', '').split('_')[-1]
        if sepisode.isnumeric():
            episodes = max(episodes, int(sepisode) + 1)

    print(f'Found existing {len(files)} files, {episodes} episodes, {steps} steps in {artifact_dir}')
    return steps, episodes


class RandomPolicy:
    def __init__(self, action_space):
        self.action_space = action_space

    def __call__(self, obs) -> Tuple[int, dict]:
        return self.action_space.sample(), {}


class NetworkPolicy:
    def __init__(self, model: Dreamer, preprocess: MinigridPreprocess):
        self.model = model
        self.preprocess = preprocess
        self._state = model.wm.init_state(1)

    def __call__(self, obs) -> Tuple[int, dict]:
        batch = self.preprocess.apply(obs, expandTB=True)

        image = torch.from_numpy(batch['image'])
        reward = torch.from_numpy(batch['reward'])
        action = torch.from_numpy(batch['action'])
        reset = torch.from_numpy(batch['reset'])

        with torch.no_grad():
            action_distr, value, new_state = self.model.forward(image, reward, action, reset, self._state)
            self._state = new_state

        action = action_distr.sample()
        action = action.argmax(-1)[0]  # one-hot => int

        metrics = dict(policy_value=value[0].item(),
                       action_prob=action_distr.probs[0, action].item(),
                       policy_entropy=action_distr.entropy()[0].item())

        return action.item(), metrics


class MinigridWanderPolicy:
    def __call__(self, obs) -> Tuple[int, dict]:
        if obs['image'].shape == (7, 7):
            (ax, ay) = (3, 6)  # agent is here
            front = MiniGrid.GRID_VALUES[obs['image'][ax, ay - 1]]  # front is up
            left = MiniGrid.GRID_VALUES[obs['image'][ax - 1, ay]]
            right = MiniGrid.GRID_VALUES[obs['image'][ax + 1, ay]]
        elif 'map_centered' in obs:
            ax = ay = obs['map_centered'].shape[0] // 2  # agent is here
            front = MiniGrid.GRID_VALUES[obs['map_centered'][ax, ay - 1]]
            left = MiniGrid.GRID_VALUES[obs['map_centered'][ax - 1, ay]]
            right = MiniGrid.GRID_VALUES[obs['map_centered'][ax + 1, ay]]
        else:
            assert False, f'Unsupported observation {obs["image"].shape}'

        empty = [1, 8]  # Empty or goal

        # Door on left => turn with 50%
        if left[0] == 4 and np.random.rand() < 0.50:
            return 0, {}

        # Door on right => turn with 50%
        if right[0] == 4 and np.random.rand() < 0.50:
            return 1, {}

        # Empty left  => turn with 10%
        if left[0] in empty and np.random.rand() < 0.10:
            return 0, {}

        # Empty right => turn with 10%
        if right[0] in empty and np.random.rand() < 0.10:
            return 1, {}

        # Closed door => open
        if front[0] == 4 and front[2] == 1:
            return 5, {}

        # Empty or open door => forward
        if front[0] in empty or (front[0] == 4 and front[2] == 0):
            return 2, {}

        # If forward blocked...

        # If wall left and not right => turn right
        if left[0] == 2 and right[0] != 2:
            return 1, {}

        # If wall right and not left => turn left
        if right[0] == 2 and left[0] != 2:
            return 0, {}

        # Left-right 50%
        if np.random.rand() < 0.50:
            return 0, {}
        else:
            return 1, {}


class MazeBouncingBallPolicy:
    # Policy:
    #   1) Forward until you hit a wall
    #   2) Turn in random 360 direction
    #   3) Go to 1)

    def __init__(self):
        self.pos = None
        self.turns_remaining = 0

    def __call__(self, obs) -> Tuple[int, dict]:
        assert 'agent_pos' in obs, f'Need agent position'
        pos = obs['agent_pos']
        action = -1

        # print(f'{self.pos} => {pos} ({obs["agent_dir"]})')

        if self.turns_remaining == 0:
            if self.pos is None or not np.all(self.pos == pos):
                # Going forward
                action = 2
                self.pos = pos
            else:
                # Hit the wall - start turning
                if np.random.randint(2) == 0:
                    # self.turns_remaining = -np.random.randint(2, 5)  # Left
                    self.turns_remaining = -1  # TODO
                else:
                    # self.turns_remaining = np.random.randint(2, 5)  # Right
                    self.turns_remaining = 1  # TODO
                self.pos = None

        if self.turns_remaining > 0:
            # Turning right
            action = 1
            self.turns_remaining -= 1

        elif self.turns_remaining < 0:
            # Turning left
            action = 0
            self.turns_remaining += 1

        assert action >= 0
        return action, {}


class MazeDijkstraPolicy:
    # Policy:
    #   1) Pick a random spot on a map
    #   2) Go there using shortest path
    #   3) Occasionally perform a random action

    def __init__(self, step_size, turn_size, epsilon=0.10):
        self.step_size = step_size
        self.turn_size = turn_size
        self.epsilon = epsilon
        self._goal = None
        self._expected_pos = None

    def __call__(self, obs) -> Tuple[int, dict]:
        assert 'agent_pos' in obs, 'Need agent position'
        assert 'map_agent' in obs, 'Need map'

        x, y = obs['agent_pos']
        dx, dy = obs['agent_dir']
        d = np.arctan2(dy, dx) / np.pi * 180
        map = obs['map_agent']
        # assert map[int(x), int(y)] >= 3, 'Agent should be here'

        if obs['reset']:
            self._goal = None  # new episode
            self._expected_pos = None
        if self._goal is None:
            self._goal = self._generate_goal(map)

        if self._expected_pos is not None:
            if not np.isclose(self._expected_pos[:2], [x, y], 1e-3).all():
                print('WARN: unexpected position - stuck? Generating new goal...')
                self._goal = self._generate_goal(map)

        while True:
            t = time.time()
            actions, path, nvis = find_shortest(map, (x, y, d), self._goal, self.step_size, self.turn_size)
            # print(f'Pos: {tuple(np.round([x,y,d], 2))}'
            #       f', Goal: {self._goal}'
            #       f', Len: {len(actions)}'
            #       f', Actions: {actions[:1]}'
            #       # f', Path: {path[:1]}'
            #       f', Visited: {nvis}'
            #       f', Time: {int((time.time()-t)*1000)}'
            #       )
            if len(actions) > 0:
                if np.random.rand() < self.epsilon:
                    self._expected_pos = None
                    return np.random.randint(3), {}  # random action
                else:
                    self._expected_pos = path[0]
                    return actions[0], {}  # best action
            else:
                self._goal = self._generate_goal(map)

    @staticmethod
    def _generate_goal(map):
        while True:
            x = np.random.randint(map.shape[0])
            y = np.random.randint(map.shape[1])
            if map[x, y] != WALL:
                return x, y


@njit
def find_shortest(map, start, goal, step_size=1.0, turn_size=45.0):
    KPREC = 5
    RADIUS = 0.2
    x, y, d = start
    gx, gy = goal

    # Well ok, this is BFS not Dijkstra, technically speaking

    que = []
    que_ix = 0
    visited = {}
    parent = {}
    parent_action = {}

    p = (x, y, d)
    key = (round(x * KPREC) / KPREC, round(y * KPREC) / KPREC, round(d * KPREC) / KPREC)
    que.append(p)
    visited[key] = True
    goal_state = None

    while que_ix < len(que):
        p = que[que_ix]
        que_ix += 1
        x, y, d = p
        if int(x) == int(gx) and int(y) == int(gy):
            goal_state = p
            break
        for action in range(3):
            x1, y1, d1 = x, y, d
            if action == 0:  # turn left
                d1 = d - turn_size
                if d1 < -180.0:
                    d1 += 360.0
            if action == 1:  # turn right
                d1 = d + turn_size
                if d1 > 180.0:
                    d1 -= 360.0
            if action == 2:  # forward
                x1 = x + step_size * np.cos(d / 180 * np.pi)
                y1 = y + step_size * np.sin(d / 180 * np.pi)
                # Check wall collision at 4 corners
                for x2, y2 in [(x1 - RADIUS, y1 - RADIUS), (x1 + RADIUS, y1 - RADIUS), (x1 - RADIUS, y1 + RADIUS), (x1 + RADIUS, y1 + RADIUS)]:
                    if x2 < 0 or y2 < 0 or x2 >= map.shape[0] or y2 >= map.shape[1] or map[int(x2), int(y2)] == WALL:
                        x1, y1 = x, y  # wall
                        break
            p1 = (x1, y1, d1)
            key = (round(x1 * KPREC) / KPREC, round(y1 * KPREC) / KPREC, round(d1 * KPREC) / KPREC)
            if key not in visited:
                que.append(p1)
                parent[p1] = p
                parent_action[p1] = action
                visited[key] = True
                assert len(visited) < 100000, 'Runaway Dijkstra'

    path = []
    actions = []
    if goal_state is not None:
        p = goal_state
        while p in parent_action:
            path.append(p)
            actions.append(parent_action[p])
            p = parent[p]
        path.reverse()
        actions.reverse()
    else:
        print('WARN: no path found')

    return actions, path, len(visited)


class ActionRewardResetWrapper:

    def __init__(self, env, max_steps: int):
        self._env = env
        self._max_steps = max_steps
        self._epstep = 0
        # Handle environments with one-hot or discrete action, but collect always as one-hot
        self._action_size = env.action_space.shape[0] if env.action_space.shape != () else env.action_space.n

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        self._epstep += 1
        obs, reward, done, info = self._env.step(action)
        if isinstance(action, int):
            action_onehot = np.zeros(self._action_size)
            action_onehot[action] = 1.0
        else:
            assert isinstance(action, np.ndarray) and action.shape == (self._action_size,), "Wrong one-hot action shape"
            action_onehot = action
        obs['action'] = action_onehot
        obs['reward'] = np.array(reward)
        # Only True if actual terminal state, not done because of max_steps
        obs['terminal'] = np.array(done if self._epstep < self._max_steps else False)
        obs['reset'] = np.array(False)
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        obs['action'] = np.zeros(self._action_size)
        obs['reward'] = np.array(0.0)
        obs['terminal'] = np.array(False)
        obs['reset'] = np.array(True)
        self._epstep = 0
        return obs


class CollectWrapper:

    def __init__(self, env):
        self._env = env
        self._episode = []

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        self._episode.append(obs.copy())
        if done:
            episode = {k: np.array([t[k] for t in self._episode]) for k in self._episode[0]}
            info['episode'] = episode
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        self._episode = [obs.copy()]
        return obs


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--env_id', type=str, required=True)
    parser.add_argument('--policy', type=str, required=True)
    parser.add_argument('--num_steps', type=int, default=1_000_000)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--env_max_steps', type=int, default=500)
    args = parser.parse_args()
    main(**vars(args))
