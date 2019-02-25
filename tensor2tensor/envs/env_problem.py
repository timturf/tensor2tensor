# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base class for envs that store their history.

EnvProblem subclasses Problem and also implements the Gym interface (step,
reset, render, close, seed)
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from gym.core import Env
import numpy as np
import six
from tensor2tensor.data_generators import generator_utils
from tensor2tensor.data_generators import problem
from tensor2tensor.envs import gym_spaces_utils
from tensor2tensor.envs import trajectory
from tensor2tensor.layers import modalities
from tensor2tensor.rl import gym_utils
import tensorflow as tf

# Names for data fields in stored tf.Examples.
TIMESTEP_FIELD = "timestep"
ACTION_FIELD = "action"
RAW_REWARD_FIELD = "raw_reward"
PROCESSED_REWARD_FIELD = "reward"
DONE_FIELD = "done"
OBSERVATION_FIELD = "observation"


class EnvProblem(Env, problem.Problem):
  """An env which generates data like a problem class.

  EnvProblem is both a gym Env and a Problem, since it subclasses both.

  Conceptually it contains `batch_size` environments on which step (and reset)
  are called. The data that is generated by the repeated application of step and
  reset is stored within this class and is persisted on disk when we call
  `generate_data` on it.

  Subclasses *should* override the following functions, since they are used in
  the `hparams` function to return modalities and vocab_sizes.
  - input_modality
  - input_input_vocab_size
  - target_modality
  - target_vocab_size

  NON NATIVELY BATCHED ENVS:

  The default implementations of the other major functions, should work well for
  cases where the env is not batched by default ex: any gym env. In this case we
  create `batch_size` number of envs and store them in a list. Any function then
  that interacts with the envs, like reset, step or close goes over the env list
  to do the needful, ex: when reset is called with specific indices we reset
  only those indices, etc.

  The usage of this class will look like the following:

  # 1. Creates and initializes the env_problem.
  ep = env_problem.EnvProblem(...)

  # 2. One needs to call reset() at the start, this resets all envs.
  ep.reset()

  # 3. Call step with actions for all envs, i.e. len(action) = batch_size
  obs, rewards, dones, infos = ep.step(actions)

  # 4. Figure out which envs got done and reset only those.
  ep.reset(indices=done_indices(dones))

  # 5. Go back to Step #3 to further interact with the env or just dump the
  # generated data to disk by calling:
  ep.generate_data(...)

  # 6. If we now need to use this object again to play a few more iterations
  # perhaps with a different batch size or maybe not recording the data, then
  # we need to re-initialize environments and do some book-keeping, call:
  ep.initialize_environments(batch_size)

  # 7. Go back to Step #2, i.e. reset all envs.

  NOTE: Look at `EnvProblemTest.test_interaction_with_env` and/or
  `EnvProblemTest.test_generate_data`

  NOTE: We rely heavily that the underlying environments expose a gym style
  interface, i.e. in addition to reset(), step() and close() we have access to
  the following properties: observation_space, action_space, reward_range.

  NATIVELY BATCHED ENVS:

  If however, our env is a neural network, which can be batched by default, we
  should:

  # 1 - Give it a gym style interface, by overriding observation_space and
  action_space.

  # 2 - Override `_reset` and `_step` to do the reset and step in a natively
  batched manner.

  # 3 - More generally any function that iterates over the self._env list will
  need to be overridden, ex: `_verify_same_spaces` and `initialize_environments`

  KNOWN LIMITATIONS:

  - observation_space and action_space should be subclasses of gym.spaces
  - not all subclasses of gym.spaces are supported
  - no support for continuous action spaces

  """

  def __init__(self,
               base_env_name=None,
               batch_size=None,
               reward_range=(-np.inf, np.inf)):
    """Initializes this class by creating the envs and managing trajectories.

    Args:
      base_env_name: (string) passed to `gym_utils.make_gym_env` to make the
        underlying environment.
      batch_size: (int or None): How many envs to make in the non natively
        batched mode.
      reward_range: (tuple(number, number)) the first element is the minimum
        reward and the second is the maximum reward, used to clip and process
        the raw reward in `process_rewards`.
    """

    # Call the super's ctor.
    problem.Problem.__init__(self, was_reversed=False, was_copy=False)

    # Name for the base environment, will be used in `gym_utils.make_gym_env` in
    # the default implementation of `initialize_environments`.
    self._base_env_name = base_env_name

    # An env generates data when it is given actions by an agent which is either
    # a policy or a human -- this is supposed to be the `id` of the agent.
    #
    # In practice, this is used only to store (and possibly retrieve) history
    # to an appropriate directory.
    self._agent_id = "default"

    # We clip rewards to this range before processing them further, as described
    # in `process_rewards`.
    self._reward_range = reward_range

    # Initialize the environment(s).

    # This can either be a list of environments of len `batch_size` or this can
    # be a Neural Network, in which case it will be fed input with first
    # dimension = `batch_size`.
    self._envs = None

    self._observation_space = None
    self._action_space = None

    # A data structure to hold the `batch_size` currently active trajectories
    # and also the ones that are completed, i.e. done.
    self._trajectories = None

    self._batch_size = None

    if batch_size is not None:
      self.initialize(batch_size=batch_size)

  @property
  def batch_size(self):
    # TODO(afrozm): I've added this here since it is being used in a lot of
    # places in ppo_learner.py -- re-evaluate if needed.
    return self._batch_size

  @property
  def base_env_name(self):
    return self._base_env_name

  @property
  def trajectories(self):
    return self._trajectories

  def _verify_same_spaces(self):
    """Verifies that all the envs have the same observation and action space."""

    # Pre-conditions: self._envs is initialized.

    if self._envs is None:
      raise ValueError("Environments not initialized.")

    if not isinstance(self._envs, list):
      tf.logging.warning("Not checking observation and action space "
                         "compatibility across envs, since there is just one.")
      return

    # NOTE: We compare string representations of observation_space and
    # action_space because compositional classes like space.Tuple don't return
    # true on object comparison.

    if not all(
        str(env.observation_space) == str(self.observation_space)
        for env in self._envs):
      err_str = ("All environments should have the same observation space, but "
                 "don't.")
      tf.logging.error(err_str)
      # Log all observation spaces.
      for i, env in enumerate(self._envs):
        tf.logging.error("Env[%d] has observation space [%s]", i,
                         env.observation_space)
      raise ValueError(err_str)

    if not all(
        str(env.action_space) == str(self.action_space) for env in self._envs):
      err_str = "All environments should have the same action space, but don't."
      tf.logging.error(err_str)
      # Log all action spaces.
      for i, env in enumerate(self._envs):
        tf.logging.error("Env[%d] has action space [%s]", i, env.action_space)
      raise ValueError(err_str)

  def initialize(self, **kwargs):
    self.initialize_environments(**kwargs)

    # Assert that *all* the above are now set, we should do this since
    # subclasses can override `initialize_environments`.
    assert self._envs is not None
    assert self._observation_space is not None
    assert self._action_space is not None
    assert self._reward_range is not None
    assert self._trajectories is not None

  def initialize_environments(self, batch_size=1, max_episode_steps=-1,
                              max_and_skip_env=False):
    """Initializes the environments and trajectories.

    Subclasses can override this if they don't want a default implementation
    which initializes `batch_size` environments, but must take care to
    initialize self._trajectories (this is checked in __init__ anyways).

    Args:
      batch_size: (int) Number of `self.base_env_name` envs to initialize.
      max_episode_steps: (int) Passed on to `gym_utils.make_gym_env`.
      max_and_skip_env: (boolean) Passed on to `gym_utils.make_gym_env`.
    """

    assert batch_size >= 1
    self._batch_size = batch_size

    self._envs = []
    for _ in range(batch_size):
      self._envs.append(
          gym_utils.make_gym_env(
              self.base_env_name,
              rl_env_max_episode_steps=max_episode_steps,
              maxskip_env=max_and_skip_env))

    # If self.observation_space and self.action_space aren't None, then it means
    # that this is a re-initialization of this class, in that case make sure
    # that this matches our previous behaviour.
    if self._observation_space:
      assert str(self._observation_space) == str(
          self._envs[0].observation_space)
    else:
      # This means that we are initializing this class for the first time.
      #
      # We set this equal to the first env's observation space, later on we'll
      # verify that all envs have the same observation space.
      self._observation_space = self._envs[0].observation_space

    # Similarly for action_space
    if self._action_space:
      assert str(self._action_space) == str(self._envs[0].action_space)
    else:
      self._action_space = self._envs[0].action_space

    self._verify_same_spaces()

    # If self.reward_range is None, i.e. this means that we should take the
    # reward range of the env.
    if self.reward_range is None:
      self._reward_range = self._envs[0].reward_range

    # This data structure stores the history of each env.
    #
    # NOTE: Even if the env is a NN and can step in all batches concurrently, it
    # is still valuable to store the trajectories separately.
    self._trajectories = trajectory.BatchTrajectory(batch_size=batch_size)

  def assert_common_preconditions(self):
    # Asserts on the common pre-conditions of:
    #  - self._envs is initialized.
    #  - self._envs is a list.
    assert self._envs
    assert isinstance(self._envs, list)

  @property
  def observation_space(self):
    return self._observation_space

  @property
  def observation_spec(self):
    """The spec for reading an observation stored in a tf.Example."""
    return gym_spaces_utils.gym_space_spec(self.observation_space)

  def process_observations(self, observations):
    """Processes observations prior to saving in the trajectories.

    Args:
      observations: (np.ndarray) observations to be processed.

    Returns:
      processed observation

    """
    return observations

  @property
  def action_space(self):
    return self._action_space

  @property
  def action_spec(self):
    """The spec for reading an observation stored in a tf.Example."""
    return gym_spaces_utils.gym_space_spec(self.action_space)

  @property
  def action_modality(self):
    raise NotImplementedError

  @property
  def num_actions(self):
    """Returns the number of actions in a discrete action space."""
    return gym_spaces_utils.cardinality(self.action_space)

  @property
  def reward_range(self):
    return self._reward_range

  @property
  def is_reward_range_finite(self):
    min_reward, max_reward = self.reward_range
    return (min_reward != -np.inf) and (max_reward != np.inf)

  def process_rewards(self, rewards):
    """Clips, rounds, and changes to integer type.

    Args:
      rewards: numpy array of raw (float) rewards.

    Returns:
      processed_rewards: numpy array of np.int64
    """

    min_reward, max_reward = self.reward_range

    # Clips at min and max reward.
    rewards = np.clip(rewards, min_reward, max_reward)
    # Round to (nearest) int and convert to integral type.
    rewards = np.around(rewards, decimals=0).astype(np.int64)
    return rewards

  @property
  def is_processed_rewards_discrete(self):
    """Returns true if `self.process_rewards` returns discrete rewards."""

    # Subclasses can override, but it should match their self.process_rewards.

    # This check is a little hackily.
    return self.process_rewards(0.0).dtype == np.int64

  @property
  def num_rewards(self):
    """Returns the number of distinct rewards.

    Returns:
      Returns None if the reward range is infinite or the processed rewards
      aren't discrete, otherwise returns the number of distinct rewards.
    """

    # Pre-conditions: reward range is finite.
    #               : processed rewards are discrete.
    if not self.is_reward_range_finite:
      tf.logging.error("Infinite reward range, `num_rewards returning None`")
      return None
    if not self.is_processed_rewards_discrete:
      tf.logging.error(
          "Processed rewards are not discrete, `num_rewards` returning None")
      return None

    min_reward, max_reward = self.reward_range
    return max_reward - min_reward + 1

  @property
  def input_modality(self):
    raise NotImplementedError

  @property
  def input_vocab_size(self):
    raise NotImplementedError

  @property
  def target_modality(self):
    raise NotImplementedError

  @property
  def target_vocab_size(self):
    raise NotImplementedError

  @property
  def unwrapped(self):
    return self

  def seed(self, seed=None):
    if not self._envs:
      tf.logging.info("`seed` called on non-existent envs, doing nothing.")
      return None

    if not isinstance(self._envs, list):
      tf.logging.warning("`seed` called on non-list envs, doing nothing.")
      return None

    tf.logging.warning(
        "Called `seed` on EnvProblem, calling seed on the underlying envs.")
    for env in self._envs:
      env.seed(seed)

    return [seed]

  def close(self):
    if not self._envs:
      tf.logging.info("`close` called on non-existent envs, doing nothing.")
      return

    if not isinstance(self._envs, list):
      tf.logging.warning("`close` called on non-list envs, doing nothing.")
      return

    # Call close on all the envs one by one.
    for env in self._envs:
      env.close()

  def _reset(self, indices):
    """Resets environments at indices shouldn't pre-process or record.

    Subclasses should override this to do the actual reset if something other
    than the default implementation is desired.

    Args:
      indices: list of indices of underlying envs to call reset on.

    Returns:
      np.ndarray of stacked observations from the reset-ed envs.
    """

    # Pre-conditions: common_preconditions, see `assert_common_preconditions`.
    self.assert_common_preconditions()

    # This returns a numpy array with first dimension `len(indices)` and the
    # rest being the dimensionality of the observation.
    return np.stack([self._envs[index].reset() for index in indices])

  def reset(self, indices=None):
    """Resets environments at given indices.

    Subclasses should override _reset to do the actual reset if something other
    than the default implementation is desired.

    Args:
      indices: Indices of environments to reset. If None all envs are reset.

    Returns:
      Batch of initial observations of reset environments.
    """

    if indices is None:
      indices = np.arange(self.trajectories.batch_size)

    # If this is empty (not None) then don't do anything, no env was done.
    if indices.size == 0:
      tf.logging.warning(
          "`reset` called with empty indices array, this is a no-op.")
      return None

    observations = self._reset(indices)
    processed_observations = self.process_observations(observations)

    # Record history.
    self.trajectories.reset(indices, observations)

    return processed_observations

  def _step(self, actions):
    """Takes a step in all environments, shouldn't pre-process or record.

    Subclasses should override this to do the actual step if something other
    than the default implementation is desired.

    Args:
      actions: (np.ndarray) with first dimension equal to the batch size.

    Returns:
      a tuple of stacked raw observations, raw rewards, dones and infos.
    """

    # Pre-conditions: common_preconditions, see `assert_common_preconditions`.
    #               : len(actions) == len(self._envs)
    self.assert_common_preconditions()
    assert len(actions) == len(self._envs)

    observations = []
    rewards = []
    dones = []
    infos = []

    # Take steps in all environments.
    for env, action in zip(self._envs, actions):
      observation, reward, done, info = env.step(action)

      observations.append(observation)
      rewards.append(reward)
      dones.append(done)
      infos.append(info)

    # Convert each list (observations, rewards, ...) into np.array and return a
    # tuple.
    return tuple(map(np.stack, [observations, rewards, dones, infos]))

  def step(self, actions):
    """Takes a step in all environments.

    Subclasses should override _step to do the actual reset if something other
    than the default implementation is desired.

    Args:
      actions: Batch of actions.

    Returns:
      (preprocessed_observations, processed_rewards, dones, infos).
    """

    observations, raw_rewards, dones, infos = self._step(actions)

    # Process rewards.
    raw_rewards = raw_rewards.astype(np.float32)
    processed_rewards = self.process_rewards(raw_rewards)

    # Process observations.
    processed_observations = self.process_observations(observations)

    # Record history.
    self.trajectories.step(processed_observations, raw_rewards,
                           processed_rewards, dones, actions)

    return processed_observations, processed_rewards, dones, infos

  @staticmethod
  def done_indices(dones):
    """Calculates the indices where dones has True."""
    return np.argwhere(dones).squeeze(axis=1)

  def example_reading_spec(self):
    """Data fields to store on disk and their decoders."""

    # Subclasses can override and/or extend.

    processed_reward_type = tf.float32
    if self.is_processed_rewards_discrete:
      processed_reward_type = tf.int64

    data_fields = {
        TIMESTEP_FIELD: tf.FixedLenFeature((1,), tf.int64),
        RAW_REWARD_FIELD: tf.FixedLenFeature((1,), tf.float32),
        PROCESSED_REWARD_FIELD: tf.FixedLenFeature((1,), processed_reward_type),
        DONE_FIELD: tf.FixedLenFeature((1,), tf.int64),  # we wrote this as int.

        # Special treatment because we need to determine type and shape, also
        # enables classes to override.
        OBSERVATION_FIELD: self.observation_spec,
        ACTION_FIELD: self.action_spec,
    }

    # `data_items_to_decoders` can be None, it will be set to the appropriate
    # decoder dict in `Problem.decode_example`
    # TODO(afrozm): Verify that we don't need any special decoder or anything.
    data_items_to_decoders = None

    return data_fields, data_items_to_decoders

  def hparams(self, defaults, model_hparams):
    # Usually when using the environment in a supervised setting, given the
    # observation we are predicting the reward.
    p = defaults

    # Have to add these the 'proper' way, otherwise __str__ doesn't show them.
    if "modality" not in p:
      p.add_hparam("modality", {})
    if "vocab_size" not in p:
      p.add_hparam("vocab_size", {})

    # TODO(afrozm): Document what all of these keys are and are supposed to do.
    p.modality.update({
        "inputs": self.input_modality,
        "targets": self.target_modality,
        "input_reward": modalities.ModalityType.SYMBOL_WEIGHTS_ALL,
        "target_reward": modalities.ModalityType.SYMBOL_WEIGHTS_ALL,
        "input_action": modalities.ModalityType.SYMBOL_WEIGHTS_ALL,
        "target_action": modalities.ModalityType.SYMBOL_WEIGHTS_ALL,
        "target_policy": modalities.ModalityType.IDENTITY,
        "target_value": modalities.ModalityType.IDENTITY,
    })

    p.vocab_size.update({
        "inputs": self.input_vocab_size,
        "targets": self.target_vocab_size,
        "input_reward": self.num_rewards,
        "target_reward": self.num_rewards,
        "input_action": self.num_actions,
        "target_action": self.num_actions,
        "target_policy": None,
        "target_value": None,
    })

    p.input_space_id = problem.SpaceID.GENERIC
    p.target_space_id = problem.SpaceID.GENERIC

  @property
  def agent_id(self):
    return self._agent_id

  @agent_id.setter
  def agent_id(self, agent_id):
    # Lets us call agent_id with integers that we increment.
    agent_id = str(agent_id)
    # We use `-` in self.dataset_filename, disallow it here for convenience.
    if "-" in agent_id:
      raise ValueError("agent_id shouldn't have - in it.")
    self._agent_id = agent_id

  def dataset_filename(self):
    return "{}-{}".format(self.name, self.agent_id)

  @property
  def num_shards(self):
    return {
        problem.DatasetSplit.TRAIN: 10,
        problem.DatasetSplit.EVAL: 1,
    }

  def _generate_time_steps(self, trajectory_list):
    """A generator to yield single time-steps from a list of trajectories."""
    for single_trajectory in trajectory_list:
      assert isinstance(single_trajectory, trajectory.Trajectory)

      # Skip writing trajectories that have only a single time-step -- this
      # could just be a repeated reset.

      if single_trajectory.num_time_steps <= 1:
        continue

      for index, time_step in enumerate(single_trajectory.time_steps):

        # The first time-step doesn't have reward/processed_reward, if so, just
        # setting it to 0.0 / 0 should be OK.
        raw_reward = time_step.raw_reward
        if not raw_reward:
          raw_reward = 0.0

        processed_reward = time_step.processed_reward
        if not processed_reward:
          processed_reward = 0

        if time_step.action:
          action = gym_spaces_utils.gym_space_encode(self.action_space,
                                                     time_step.action)
        else:
          # The last time-step doesn't have action, and this action shouldn't be
          # used, gym's spaces have a `sample` function, so let's just sample an
          # action and use that.
          action = [self.action_space.sample()]

        if six.PY3:
          # py3 complains that, to_example cannot handle np.int64 !

          action_dtype = self.action_space.dtype
          if action_dtype in [np.int64, np.int32]:
            action = list(map(int, action))
          elif action_dtype in [np.float64, np.float32]:
            action = list(map(float, action))

          # same with processed_reward.
          processed_reward = int(processed_reward)

        assert time_step.observation is not None

        yield {
            TIMESTEP_FIELD: [index],
            ACTION_FIELD: action,
            # to_example errors on np.float32
            RAW_REWARD_FIELD: [float(raw_reward)],
            PROCESSED_REWARD_FIELD: [processed_reward],
            # to_example doesn't know bools
            DONE_FIELD: [int(time_step.done)],
            OBSERVATION_FIELD:
                gym_spaces_utils.gym_space_encode(self.observation_space,
                                                  time_step.observation),
        }

  def generate_data(self, data_dir, tmp_dir, task_id=-1):
    # List of files to generate data in.
    # NOTE: We don't want to shuffle, so we mark the files as shuffled.
    files_list = []
    for split, num_shards in self.num_shards.items():
      files_list.extend(self.data_filepaths(split, data_dir, num_shards, True))

    # At this point some trajectories haven't finished. However we still want to
    # write those down.

    # A simple way of doing this is to call `self.reset()` here, this will make
    # all the envs take one (extra) step, but would be a clean way to do it.
    #
    # self.reset()

    self.trajectories.complete_all_trajectories()

    # Write the completed data into these files

    num_completed_trajectories = len(self.trajectories.completed_trajectories)
    num_shards = len(files_list)
    if num_completed_trajectories < num_shards:
      tf.logging.warning(
          "Number of completed trajectories [%d] is less than "
          "the number of shards [%d], some shards maybe empty.",
          num_completed_trajectories, num_shards)

    for i, f in enumerate(files_list[:num_completed_trajectories]):
      # Start at index i of completed trajectories and take every `num_shards`
      # trajectory. This ensures that the data is approximately a balanced
      # partition of completed trajectories, also because of the above slicing
      # of files_list, i will be a valid index into completed_trajectories.
      trajectories_to_write = self.trajectories.completed_trajectories[
          i::num_shards]

      # Convert each trajectory from `trajectories_to_write` to a sequence of
      # time-steps and then send that generator to `generate_files`.

      # `cycle_every_n` isn't needed since file list given to it is a singleton.
      generator_utils.generate_files(
          self._generate_time_steps(trajectories_to_write), [f])

  def print_state(self):
    for t in self.trajectories.trajectories:
      print("---------")
      if not t.is_active:
        print("trajectory isn't active.")
        continue
      last_obs = t.last_time_step.observation
      print(str(last_obs))
