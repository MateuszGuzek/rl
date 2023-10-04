# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import os
import urllib
from pathlib import Path
import warnings
from typing import Callable, Optional

import numpy as np

import torch

from tensordict import PersistentTensorDict
from tensordict.tensordict import make_tensordict, TensorDict

from torchrl.collectors.utils import split_trajectories
from torchrl.data.replay_buffers import TensorDictReplayBuffer
from torchrl.data.replay_buffers.samplers import Sampler
from torchrl.data.replay_buffers.storages import LazyMemmapStorage
from torchrl.data.replay_buffers.writers import Writer


class D4RLExperienceReplay(TensorDictReplayBuffer):
    """An Experience replay class for D4RL.

    To install D4RL, follow the instructions on the
    `official repo <https://github.com/Farama-Foundation/D4RL>`__.

    The replay buffer contains the env specs under D4RLExperienceReplay.specs.

    If present, metadata will be written in ``D4RLExperienceReplay.metadata``
    and excluded from the dataset.

    The transitions are reconstructed using ``done = terminated | truncated`` and
    the ``("next", "observation")`` of ``"done"`` states are zeroed.

    Args:
        name (str): the name of the D4RL env to get the data from.
        batch_size (int): the batch size to use during sampling.
        sampler (Sampler, optional): the sampler to be used. If none is provided
            a default RandomSampler() will be used.
        writer (Writer, optional): the writer to be used. If none is provided
            a default RoundRobinWriter() will be used.
        collate_fn (callable, optional): merges a list of samples to form a
            mini-batch of Tensor(s)/outputs.  Used when using batched
            loading from a map-style dataset.
        pin_memory (bool): whether pin_memory() should be called on the rb
            samples.
        prefetch (int, optional): number of next batches to be prefetched
            using multithreading.
        transform (Transform, optional): Transform to be executed when sample() is called.
            To chain transforms use the :obj:`Compose` class.
        split_trajs (bool, optional): if ``True``, the trajectories will be split
            along the first dimension and padded to have a matching shape.
            To split the trajectories, the ``"done"`` signal will be used, which
            is recovered via ``done = truncated | terminated``. In other words,
            it is assumed that any ``truncated`` or ``terminated`` signal is
            equivalent to the end of a trajectory. For some datasets from
            ``D4RL``, this may not be true. It is up to the user to make
            accurate choices regarding this usage of ``split_trajs``.
            Defaults to ``False``.
        from_env (bool, optional): if ``True``, :meth:`env.get_dataset` will
            be used to retrieve the dataset. Otherwise :func:`d4rl.qlearning_dataset`
            will be used. Defaults to ``True``.

            .. note::

              Using ``from_env=False`` will provide less data than ``from_env=True``.
              For instance, the info keys will be left out.
              Usually, ``from_env=False`` with ``terminate_on_end=True`` will
              lead to the same result as ``from_env=True``, with the latter
              containing meta-data and info entries that the former does
              not possess.

            .. note::

              The keys in ``from_env=True`` and ``from_env=False`` *may* unexpectedly
              differ. In particular, the ``"truncated"`` key (used to determine the
              end of an episode) may be absent when ``from_env=False`` but present
              otherwise, leading to a different slicing when ``traj_splits`` is enabled.
        direct_download (bool): if ``True`` (default), the data will be downloaded without
            requiring D4RL. This is not compatible with ``from_env=True``.
        use_truncated_as_done (bool, optional): if ``True``, ``done = terminated | truncated``.
            Otherwise, only the ``terminated`` key is used. Defaults to ``True``.
        **env_kwargs (key-value pairs): additional kwargs for
            :func:`d4rl.qlearning_dataset`. Supports ``terminate_on_end``
            (``False`` by default) or other kwargs if defined by D4RL library.


    Examples:
        >>> from torchrl.data.datasets.d4rl import D4RLExperienceReplay
        >>> from torchrl.envs import ObservationNorm
        >>> data = D4RLExperienceReplay("maze2d-umaze-v1", 128)
        >>> # we can append transforms to the dataset
        >>> data.append_transform(ObservationNorm(loc=-1, scale=1.0))
        >>> data.sample(128)

    """

    D4RL_DATASETS = {
        "maze2d-open-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-open-sparse.hdf5",
        "maze2d-umaze-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-umaze-sparse-v1.hdf5",
        "maze2d-medium-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-medium-sparse-v1.hdf5",
        "maze2d-large-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-large-sparse-v1.hdf5",
        "maze2d-eval-umaze-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-eval-umaze-sparse-v1.hdf5",
        "maze2d-eval-medium-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-eval-medium-sparse-v1.hdf5",
        "maze2d-eval-large-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-eval-large-sparse-v1.hdf5",
        "maze2d-open-dense-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-open-dense.hdf5",
        "maze2d-umaze-dense-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-umaze-dense-v1.hdf5",
        "maze2d-medium-dense-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-medium-dense-v1.hdf5",
        "maze2d-large-dense-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-large-dense-v1.hdf5",
        "maze2d-eval-umaze-dense-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-eval-umaze-dense-v1.hdf5",
        "maze2d-eval-medium-dense-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-eval-medium-dense-v1.hdf5",
        "maze2d-eval-large-dense-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-eval-large-dense-v1.hdf5",
        "minigrid-fourrooms-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/minigrid/minigrid4rooms.hdf5",
        "minigrid-fourrooms-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/minigrid/minigrid4rooms_random.hdf5",
        "pen-human-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/pen-v0_demos_clipped.hdf5",
        "pen-cloned-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/pen-demos-v0-bc-combined.hdf5",
        "pen-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/pen-v0_expert_clipped.hdf5",
        "hammer-human-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/hammer-v0_demos_clipped.hdf5",
        "hammer-cloned-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/hammer-demos-v0-bc-combined.hdf5",
        "hammer-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/hammer-v0_expert_clipped.hdf5",
        "relocate-human-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/relocate-v0_demos_clipped.hdf5",
        "relocate-cloned-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/relocate-demos-v0-bc-combined.hdf5",
        "relocate-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/relocate-v0_expert_clipped.hdf5",
        "door-human-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/door-v0_demos_clipped.hdf5",
        "door-cloned-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/door-demos-v0-bc-combined.hdf5",
        "door-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/door-v0_expert_clipped.hdf5",
        "halfcheetah-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/halfcheetah_random.hdf5",
        "halfcheetah-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/halfcheetah_medium.hdf5",
        "halfcheetah-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/halfcheetah_expert.hdf5",
        "halfcheetah-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/halfcheetah_mixed.hdf5",
        "halfcheetah-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/halfcheetah_medium_expert.hdf5",
        "walker2d-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/walker2d_random.hdf5",
        "walker2d-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/walker2d_medium.hdf5",
        "walker2d-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/walker2d_expert.hdf5",
        "walker2d-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/walker_mixed.hdf5",
        "walker2d-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/walker2d_medium_expert.hdf5",
        "hopper-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/hopper_random.hdf5",
        "hopper-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/hopper_medium.hdf5",
        "hopper-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/hopper_expert.hdf5",
        "hopper-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/hopper_mixed.hdf5",
        "hopper-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/hopper_medium_expert.hdf5",
        "ant-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/ant_random.hdf5",
        "ant-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/ant_medium.hdf5",
        "ant-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/ant_expert.hdf5",
        "ant-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/ant_mixed.hdf5",
        "ant-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/ant_medium_expert.hdf5",
        "ant-random-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco/ant_random_expert.hdf5",
        "antmaze-umaze-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_u-maze_noisy_multistart_False_multigoal_False_sparse.hdf5",
        "antmaze-umaze-diverse-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_u-maze_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-medium-play-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_big-maze_noisy_multistart_True_multigoal_False_sparse.hdf5",
        "antmaze-medium-diverse-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_big-maze_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-large-play-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_hardest-maze_noisy_multistart_True_multigoal_False_sparse.hdf5",
        "antmaze-large-diverse-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_hardest-maze_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-umaze-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v2/Ant_maze_u-maze_noisy_multistart_False_multigoal_False_sparse_fixed.hdf5",
        "antmaze-umaze-diverse-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v2/Ant_maze_u-maze_noisy_multistart_True_multigoal_True_sparse_fixed.hdf5",
        "antmaze-medium-play-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v2/Ant_maze_big-maze_noisy_multistart_True_multigoal_False_sparse_fixed.hdf5",
        "antmaze-medium-diverse-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v2/Ant_maze_big-maze_noisy_multistart_True_multigoal_True_sparse_fixed.hdf5",
        "antmaze-large-play-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v2/Ant_maze_hardest-maze_noisy_multistart_True_multigoal_False_sparse_fixed.hdf5",
        "antmaze-large-diverse-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v2/Ant_maze_hardest-maze_noisy_multistart_True_multigoal_True_sparse_fixed.hdf5",
        "flow-ring-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/flow/flow-ring-v0-random.hdf5",
        "flow-ring-controller-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/flow/flow-ring-v0-idm.hdf5",
        "flow-merge-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/flow/flow-merge-v0-random.hdf5",
        "flow-merge-controller-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/flow/flow-merge-v0-idm.hdf5",
        "kitchen-complete-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/kitchen/mini_kitchen_microwave_kettle_light_slider-v0.hdf5",
        "kitchen-partial-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/kitchen/kitchen_microwave_kettle_light_slider-v0.hdf5",
        "kitchen-mixed-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/kitchen/kitchen_microwave_kettle_bottomburner_light-v0.hdf5",
        "carla-lane-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/carla/carla_lane_follow_flat-v0.hdf5",
        "carla-town-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/carla/carla_town_subsamp_flat-v0.hdf5",
        "carla-town-full-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/carla/carla_town_flat-v0.hdf5",
        "bullet-halfcheetah-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-halfcheetah_random.hdf5",
        "bullet-halfcheetah-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-halfcheetah_medium.hdf5",
        "bullet-halfcheetah-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-halfcheetah_expert.hdf5",
        "bullet-halfcheetah-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-halfcheetah_medium_expert.hdf5",
        "bullet-halfcheetah-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-halfcheetah_medium_replay.hdf5",
        "bullet-hopper-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-hopper_random.hdf5",
        "bullet-hopper-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-hopper_medium.hdf5",
        "bullet-hopper-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-hopper_expert.hdf5",
        "bullet-hopper-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-hopper_medium_expert.hdf5",
        "bullet-hopper-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-hopper_medium_replay.hdf5",
        "bullet-ant-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-ant_random.hdf5",
        "bullet-ant-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-ant_medium.hdf5",
        "bullet-ant-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-ant_expert.hdf5",
        "bullet-ant-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-ant_medium_expert.hdf5",
        "bullet-ant-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-ant_medium_replay.hdf5",
        "bullet-walker2d-random-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-walker2d_random.hdf5",
        "bullet-walker2d-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-walker2d_medium.hdf5",
        "bullet-walker2d-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-walker2d_expert.hdf5",
        "bullet-walker2d-medium-expert-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-walker2d_medium_expert.hdf5",
        "bullet-walker2d-medium-replay-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-walker2d_medium_replay.hdf5",
        "bullet-maze2d-open-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-maze2d-open-sparse.hdf5",
        "bullet-maze2d-umaze-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-maze2d-umaze-sparse.hdf5",
        "bullet-maze2d-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-maze2d-medium-sparse.hdf5",
        "bullet-maze2d-large-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/bullet/bullet-maze2d-large-sparse.hdf5",
        "halfcheetah-random-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/halfcheetah_random-v1.hdf5",
        "halfcheetah-random-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/halfcheetah_random-v2.hdf5",
        "halfcheetah-medium-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/halfcheetah_medium-v1.hdf5",
        "halfcheetah-medium-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/halfcheetah_medium-v2.hdf5",
        "halfcheetah-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/halfcheetah_expert-v1.hdf5",
        "halfcheetah-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/halfcheetah_expert-v2.hdf5",
        "halfcheetah-medium-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/halfcheetah_medium_replay-v1.hdf5",
        "halfcheetah-medium-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/halfcheetah_medium_replay-v2.hdf5",
        "halfcheetah-full-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/halfcheetah_full_replay-v1.hdf5",
        "halfcheetah-full-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/halfcheetah_full_replay-v2.hdf5",
        "halfcheetah-medium-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/halfcheetah_medium_expert-v1.hdf5",
        "halfcheetah-medium-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/halfcheetah_medium_expert-v2.hdf5",
        "hopper-random-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/hopper_random-v1.hdf5",
        "hopper-random-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/hopper_random-v2.hdf5",
        "hopper-medium-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/hopper_medium-v1.hdf5",
        "hopper-medium-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/hopper_medium-v2.hdf5",
        "hopper-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/hopper_expert-v1.hdf5",
        "hopper-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/hopper_expert-v2.hdf5",
        "hopper-medium-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/hopper_medium_replay-v1.hdf5",
        "hopper-medium-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/hopper_medium_replay-v2.hdf5",
        "hopper-full-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/hopper_full_replay-v1.hdf5",
        "hopper-full-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/hopper_full_replay-v2.hdf5",
        "hopper-medium-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/hopper_medium_expert-v1.hdf5",
        "hopper-medium-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/hopper_medium_expert-v2.hdf5",
        "walker2d-random-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/walker2d_random-v1.hdf5",
        "walker2d-random-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/walker2d_random-v2.hdf5",
        "walker2d-medium-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/walker2d_medium-v1.hdf5",
        "walker2d-medium-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/walker2d_medium-v2.hdf5",
        "walker2d-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/walker2d_expert-v1.hdf5",
        "walker2d-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/walker2d_expert-v2.hdf5",
        "walker2d-medium-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/walker2d_medium_replay-v1.hdf5",
        "walker2d-medium-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/walker2d_medium_replay-v2.hdf5",
        "walker2d-full-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/walker2d_full_replay-v1.hdf5",
        "walker2d-full-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/walker2d_full_replay-v2.hdf5",
        "walker2d-medium-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/walker2d_medium_expert-v1.hdf5",
        "walker2d-medium-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/walker2d_medium_expert-v2.hdf5",
        "ant-random-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/ant_random-v1.hdf5",
        "ant-random-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/ant_random-v2.hdf5",
        "ant-medium-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/ant_medium-v1.hdf5",
        "ant-medium-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/ant_medium-v2.hdf5",
        "ant-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/ant_expert-v1.hdf5",
        "ant-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/ant_expert-v2.hdf5",
        "ant-medium-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/ant_medium_replay-v1.hdf5",
        "ant-medium-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/ant_medium_replay-v2.hdf5",
        "ant-full-replay-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/ant_full_replay-v1.hdf5",
        "ant-full-replay-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/ant_full_replay-v2.hdf5",
        "ant-medium-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v1/ant_medium_expert-v1.hdf5",
        "ant-medium-expert-v2": "http://rail.eecs.berkeley.edu/datasets/offline_rl/gym_mujoco_v2/ant_medium_expert-v2.hdf5",
        "hammer-human-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/hammer-human-v1.hdf5",
        "hammer-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/hammer-expert-v1.hdf5",
        "hammer-cloned-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/hammer-cloned-v1.hdf5",
        "pen-human-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/pen-human-v1.hdf5",
        "pen-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/pen-expert-v1.hdf5",
        "pen-cloned-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/pen-cloned-v1.hdf5",
        "relocate-human-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/relocate-human-v1.hdf5",
        "relocate-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/relocate-expert-v1.hdf5",
        "relocate-cloned-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/relocate-cloned-v1.hdf5",
        "door-human-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/door-human-v1.hdf5",
        "door-expert-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/door-expert-v1.hdf5",
        "door-cloned-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg_v1/door-cloned-v1.hdf5",
        "antmaze-umaze-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v1/Ant_maze_umaze_noisy_multistart_False_multigoal_False_sparse.hdf5",
        "antmaze-umaze-diverse-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v1/Ant_maze_umaze_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-medium-play-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v1/Ant_maze_medium_noisy_multistart_True_multigoal_False_sparse.hdf5",
        "antmaze-medium-diverse-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v1/Ant_maze_medium_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-large-diverse-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v1/Ant_maze_large_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-large-play-v1": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_v1/Ant_maze_large_noisy_multistart_True_multigoal_False_sparse.hdf5",
        "antmaze-eval-umaze-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_umaze_eval_noisy_multistart_True_multigoal_False_sparse.hdf5",
        "antmaze-eval-umaze-diverse-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_umaze_eval_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-eval-medium-play-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_medium_eval_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "antmaze-eval-medium-diverse-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_medium_eval_noisy_multistart_True_multigoal_False_sparse.hdf5",
        "antmaze-eval-large-diverse-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_large_eval_noisy_multistart_True_multigoal_False_sparse.hdf5",
        "antmaze-eval-large-play-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/ant_maze_new/Ant_maze_large_eval_noisy_multistart_True_multigoal_True_sparse.hdf5",
        "door-human-longhorizon-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/door-v0_demos_clipped.hdf5",
        "hammer-human-longhorizon-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/hammer-v0_demos_clipped.hdf5",
        "pen-human-longhorizon-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/pen-v0_demos_clipped.hdf5",
        "relocate-human-longhorizon-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/hand_dapg/relocate-v0_demos_clipped.hdf5",
        "maze2d-umaze-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-umaze-sparse.hdf5",
        "maze2d-medium-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-medium-sparse.hdf5",
        "maze2d-large-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-large-sparse.hdf5",
        "maze2d-umaze-dense-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-umaze-dense.hdf5",
        "maze2d-medium-dense-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-medium-dense.hdf5",
        "maze2d-large-dense-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/maze2d/maze2d-large-dense.hdf5",
        "carla-lane-render-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/carla/carla_lane_follow-v0.hdf5",
        "carla-town-render-v0": "http://rail.eecs.berkeley.edu/datasets/offline_rl/carla/carla_town_flat-v0.hdf5",
    }

    D4RL_ERR = None

    @classmethod
    def _import_d4rl(cls):
        try:
            import d4rl  # noqa

            cls._has_d4rl = True
        except ModuleNotFoundError as err:
            cls._has_d4rl = False
            cls.D4RL_ERR = err

    def __init__(
        self,
        name,
        batch_size: int,
        sampler: Optional[Sampler] = None,
        writer: Optional[Writer] = None,
        collate_fn: Optional[Callable] = None,
        pin_memory: bool = False,
        prefetch: Optional[int] = None,
        transform: Optional["Transform"] = None,  # noqa-F821
        split_trajs: bool = False,
        from_env: bool = True,
        use_truncated_as_done: bool = True,
        direct_download: bool = True,
        **env_kwargs,
    ):
        self.from_env = from_env
        self.use_truncated_as_done = use_truncated_as_done
        if not direct_download:
            self._import_d4rl()

            if not self._has_d4rl:
                raise ImportError("Could not import d4rl") from self.D4RL_ERR

            if from_env:
                dataset = self._get_dataset_from_env(name, env_kwargs)
            else:
                dataset = self._get_dataset_direct(name, env_kwargs)
        else:
            dataset = self._get_dataset_direct_download(name, env_kwargs)
        # Fill unknown next states with 0
        dataset["next", "observation"][dataset["next", "done"].squeeze()] = 0

        if split_trajs:
            dataset = split_trajectories(dataset)
            dataset["next", "done"][:, -1] = True

        storage = LazyMemmapStorage(dataset.shape[0])
        super().__init__(
            batch_size=batch_size,
            storage=storage,
            sampler=sampler,
            writer=writer,
            collate_fn=collate_fn,
            pin_memory=pin_memory,
            prefetch=prefetch,
            transform=transform,
        )
        self.extend(dataset)

    def _get_dataset_direct_download(self, name, env_kwargs):
        """Directly download and use a D4RL dataset."""
        if env_kwargs:
            raise RuntimeError("Cannot pass env_kwargs when `direct_download=True`.")
        url = self.D4RL_DATASETS.get(name, None)
        if url is None:
            raise KeyError(f"Env {name} not found.")
        h5path = _download_dataset_from_url(url)
        # h5path_parent = Path(h5path).parent
        dataset = PersistentTensorDict.from_h5(h5path)
        dataset = dataset.to_tensordict()
        with dataset.unlock_():
            dataset = self._process_data_from_env(dataset)
        return dataset

    def _get_dataset_direct(self, name, env_kwargs):
        from torchrl.envs.libs.gym import GymWrapper

        type(self)._import_d4rl()

        if not self._has_d4rl:
            raise ImportError("Could not import d4rl") from self.D4RL_ERR
        import d4rl
        import gym

        env = GymWrapper(gym.make(name))
        dataset = d4rl.qlearning_dataset(env._env, **env_kwargs)

        dataset = make_tensordict(
            {
                k: torch.from_numpy(item)
                for k, item in dataset.items()
                if isinstance(item, np.ndarray)
            }
        )
        dataset = dataset.unflatten_keys("/")
        if "metadata" in dataset.keys():
            metadata = dataset.get("metadata")
            dataset = dataset.exclude("metadata")
            self.metadata = metadata
            # find batch size
            dataset = make_tensordict(dataset.flatten_keys("/").to_dict())
            dataset = dataset.unflatten_keys("/")
        else:
            self.metadata = {}
        dataset.rename_key_("observations", "observation")
        dataset.set("next", dataset.select())
        dataset.rename_key_("next_observations", ("next", "observation"))
        dataset.rename_key_("terminals", "terminated")
        if "timeouts" in dataset.keys():
            dataset.rename_key_("timeouts", "truncated")
        if self.use_truncated_as_done:
            done = dataset.get("terminated") | dataset.get("truncated", False)
            dataset.set("done", done)
        else:
            dataset.set("done", dataset.get("terminated"))
        dataset.rename_key_("rewards", "reward")
        dataset.rename_key_("actions", "action")

        # let's make sure that the dtypes match what's expected
        for key, spec in env.observation_spec.items(True, True):
            dataset[key] = dataset[key].to(spec.dtype)
            dataset["next", key] = dataset["next", key].to(spec.dtype)
        dataset["action"] = dataset["action"].to(env.action_spec.dtype)
        dataset["reward"] = dataset["reward"].to(env.reward_spec.dtype)

        # format done etc
        dataset["done"] = dataset["done"].bool().unsqueeze(-1)
        dataset["terminated"] = dataset["terminated"].bool().unsqueeze(-1)
        if "truncated" in dataset.keys():
            dataset["truncated"] = dataset["truncated"].bool().unsqueeze(-1)
        # dataset.rename_key_("next_observations", "next/observation")
        dataset["reward"] = dataset["reward"].unsqueeze(-1)
        dataset["next"].update(
            dataset.select("reward", "done", "terminated", "truncated", strict=False)
        )
        dataset = (
            dataset.clone()
        )  # make sure that all tensors have a different data_ptr
        self._shift_reward_done(dataset)
        self.specs = env.specs.clone()
        return dataset

    def _get_dataset_from_env(self, name, env_kwargs):
        """Creates an environment and retrieves the dataset using env.get_dataset().

        This method does not accept extra arguments.

        """
        if env_kwargs:
            raise RuntimeError("env_kwargs cannot be passed with using from_env=True")
        import gym

        # we do a local import to avoid circular import issues
        from torchrl.envs.libs.gym import GymWrapper

        env = GymWrapper(gym.make(name))
        dataset = make_tensordict(
            {
                k: torch.from_numpy(item)
                for k, item in env.get_dataset().items()
                if isinstance(item, np.ndarray)
            }
        )
        dataset = dataset.unflatten_keys("/")
        dataset = self._process_data_from_env(dataset, env)
        return dataset

    def _process_data_from_env(self, dataset, env=None):
        if "metadata" in dataset.keys():
            metadata = dataset.get("metadata")
            dataset = dataset.exclude("metadata")
            self.metadata = metadata
            # find batch size
            dataset = make_tensordict(dataset.flatten_keys("/").to_dict())
            dataset = dataset.unflatten_keys("/")
        else:
            self.metadata = {}

        dataset.rename_key_("observations", "observation")
        dataset.rename_key_("terminals", "terminated")
        if "timeouts" in dataset.keys():
            dataset.rename_key_("timeouts", "truncated")
        if self.use_truncated_as_done:
            dataset.set(
                "done",
                dataset.get("terminated") | dataset.get("truncated", False),
            )
        else:
            dataset.set("done", dataset.get("terminated"))

        dataset.rename_key_("rewards", "reward")
        dataset.rename_key_("actions", "action")
        try:
            dataset.rename_key_("infos", "info")
        except KeyError:
            pass

        # let's make sure that the dtypes match what's expected
        if env is not None:
            for key, spec in env.observation_spec.items(True, True):
                dataset[key] = dataset[key].to(spec.dtype)
            dataset["action"] = dataset["action"].to(env.action_spec.dtype)
            dataset["reward"] = dataset["reward"].to(env.reward_spec.dtype)

        # format done
        dataset["done"] = dataset["done"].bool().unsqueeze(-1)
        dataset["terminated"] = dataset["terminated"].bool().unsqueeze(-1)
        if "truncated" in dataset.keys():
            dataset["truncated"] = dataset["truncated"].bool().unsqueeze(-1)

        dataset["reward"] = dataset["reward"].unsqueeze(-1)
        dataset = dataset[:-1].set(
            "next",
            dataset.select("observation", "info", strict=False)[1:],
        )
        dataset["next"].update(
            dataset.select("reward", "done", "terminated", "truncated", strict=False)
        )
        dataset = (
            dataset.clone()
        )  # make sure that all tensors have a different data_ptr
        self._shift_reward_done(dataset)
        if env is not None:
            self.specs = env.specs.clone()
        else:
            self.specs = None
        return dataset

    def _shift_reward_done(self, dataset):
        dataset["reward"] = dataset["reward"].clone()
        dataset["reward"][1:] = dataset["reward"][:-1].clone()
        dataset["reward"][0] = 0
        for key in ("done", "terminated", "truncated"):
            if key not in dataset.keys():
                continue
            dataset[key] = dataset[key].clone()
            dataset[key][1:] = dataset[key][:-1].clone()
            dataset[key][0] = 0


def _download_dataset_from_url(dataset_url):
    dataset_filepath = _filepath_from_url(dataset_url)
    if not os.path.exists(dataset_filepath):
        print("Downloading dataset:", dataset_url, "to", dataset_filepath)
        urllib.request.urlretrieve(dataset_url, dataset_filepath)
    if not os.path.exists(dataset_filepath):
        raise IOError("Failed to download dataset from %s" % dataset_url)
    return dataset_filepath


def _filepath_from_url(dataset_url):
    _, dataset_name = os.path.split(dataset_url)
    dataset_filepath = os.path.join(DATASET_PATH, dataset_name)
    return dataset_filepath


def _set_dataset_path(path):
    global DATASET_PATH
    DATASET_PATH = path
    os.makedirs(path, exist_ok=True)


_set_dataset_path(
    os.environ.get(
        "D4RL_DATASET_DIR", os.path.expanduser("~/.cache/torchrl/data/d4rl/datasets")
    )
)

if __name__ == "__main__":
    data = D4RLExperienceReplay("kitchen-partial-v0", batch_size=128)
    print(data)
    for sample in data:
        print(sample)
        break
