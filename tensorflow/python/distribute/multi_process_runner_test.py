# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Tests for `multi_process_runner`."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import ctypes
import json
import os
import sys
import threading
import time
import unittest

from absl import logging

from tensorflow.python.distribute import multi_process_runner
from tensorflow.python.distribute import multi_worker_test_base
from tensorflow.python.eager import test


def proc_func_that_adds_task_type_in_return_data():
  return multi_worker_test_base.get_task_type()


def proc_func_that_errors():
  raise ValueError('This is an error.')


def proc_func_that_does_nothing():
  pass


def proc_func_that_adds_simple_return_data():
  return 'dummy_data'


def proc_func_that_returns_args_and_kwargs(*args, **kwargs):
  return list(args) + list(kwargs.items())


def proc_func_with_barrier():
  return multi_process_runner.barrier()


def proc_func_that_returns_pid():
  return os.getpid()


V = None


def proc_func_that_sets_global(val):
  global V
  old_val = V
  V = val
  return old_val


class MultiProcessRunnerTest(test.TestCase):

  def _worker_idx(self):
    config_task = json.loads(os.environ['TF_CONFIG'])['task']
    return config_task['index']

  def test_multi_process_runner(self):
    mpr_result = multi_process_runner.run(
        proc_func_that_adds_task_type_in_return_data,
        multi_worker_test_base.create_cluster_spec(
            num_workers=2, num_ps=3, has_eval=1))

    job_count_dict = {'worker': 2, 'ps': 3, 'evaluator': 1}
    for data in mpr_result.return_value:
      job_count_dict[data] -= 1

    self.assertEqual(job_count_dict['worker'], 0)
    self.assertEqual(job_count_dict['ps'], 0)
    self.assertEqual(job_count_dict['evaluator'], 0)

  def test_multi_process_runner_error_propagates_from_subprocesses(self):
    runner = multi_process_runner.MultiProcessRunner(
        proc_func_that_errors,
        multi_worker_test_base.create_cluster_spec(num_workers=1, num_ps=1),
        max_run_time=20)
    runner.start()
    with self.assertRaisesRegex(ValueError, 'This is an error.'):
      runner.join()

  def test_multi_process_runner_queue_emptied_between_runs(self):
    cluster_spec = multi_worker_test_base.create_cluster_spec(num_workers=2)
    return_value = multi_process_runner.run(
        proc_func_that_adds_simple_return_data, cluster_spec).return_value
    self.assertTrue(return_value)
    self.assertEqual(return_value[0], 'dummy_data')
    self.assertEqual(return_value[1], 'dummy_data')
    return_value = multi_process_runner.run(proc_func_that_does_nothing,
                                            cluster_spec).return_value
    self.assertFalse(return_value)

  def test_multi_process_runner_args_passed_correctly(self):
    return_value = multi_process_runner.run(
        proc_func_that_returns_args_and_kwargs,
        multi_worker_test_base.create_cluster_spec(num_workers=1),
        args=('a', 'b'),
        kwargs={
            'c_k': 'c_v'
        }).return_value
    self.assertEqual(return_value[0][0], 'a')
    self.assertEqual(return_value[0][1], 'b')
    self.assertEqual(return_value[0][2], ('c_k', 'c_v'))

  def test_stdout_captured(self):

    def simple_print_func():
      print('This is something printed.', flush=True)
      return 'This is returned data.'

    mpr_result = multi_process_runner.run(
        simple_print_func,
        multi_worker_test_base.create_cluster_spec(num_workers=2),
        list_stdout=True)
    std_stream_results = mpr_result.stdout
    return_value = mpr_result.return_value
    self.assertIn('[worker-0]:    This is something printed.\n',
                  std_stream_results)
    self.assertIn('[worker-1]:    This is something printed.\n',
                  std_stream_results)
    self.assertIn('This is returned data.', return_value)

  def test_termination(self):

    def proc_func():
      for i in range(0, 10):
        print(
            'index {}, iteration {}'.format(self._worker_idx(), i), flush=True)
        time.sleep(5)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(num_workers=2),
        list_stdout=True)
    mpr.start()
    time.sleep(5)
    mpr.terminate('worker', 0)

    std_stream_results = mpr.join().stdout

    # Worker 0 is terminated in the middle, so it should not have iteration 9
    # printed.
    self.assertIn('[worker-0]:    index 0, iteration 0\n', std_stream_results)
    self.assertNotIn('[worker-0]:    index 0, iteration 9\n',
                     std_stream_results)
    self.assertIn('[worker-1]:    index 1, iteration 0\n', std_stream_results)
    self.assertIn('[worker-1]:    index 1, iteration 9\n', std_stream_results)

  def test_termination_and_start_single_process(self):

    def proc_func():
      for i in range(0, 10):
        print(
            'index {}, iteration {}'.format(self._worker_idx(), i), flush=True)
        time.sleep(1)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(num_workers=2),
        list_stdout=True)
    mpr.start()
    time.sleep(3)
    mpr.terminate('worker', 0)
    mpr.start_single_process('worker', 0)
    std_stream_results = mpr.join().stdout

    # Worker 0 is terminated in the middle, but a new worker 0 is added, so it
    # should still have iteration 9 printed. Moreover, iteration 0 of worker 0
    # should happen twice.
    self.assertLen(
        [s for s in std_stream_results if 'index 0, iteration 0' in s], 2)
    self.assertIn('[worker-0]:    index 0, iteration 9\n', std_stream_results)
    self.assertIn('[worker-1]:    index 1, iteration 0\n', std_stream_results)
    self.assertIn('[worker-1]:    index 1, iteration 9\n', std_stream_results)

  def test_streaming(self):

    def proc_func():
      for i in range(5):
        logging.info('(logging) %s-%d, i: %d',
                     multi_worker_test_base.get_task_type(), self._worker_idx(),
                     i)
        print(
            '(print) {}-{}, i: {}'.format(
                multi_worker_test_base.get_task_type(), self._worker_idx(), i),
            flush=True)
        time.sleep(1)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(
            has_chief=True, num_workers=2, num_ps=2, has_eval=True),
        list_stdout=True)
    mpr._dependence_on_chief = False

    mpr.start()
    mpr.start_single_process('worker', 2)
    mpr.start_single_process('ps', 2)
    mpr_result = mpr.join()

    list_to_assert = mpr_result.stdout

    for job in ['chief', 'evaluator']:
      for iteration in range(5):
        self.assertTrue(
            any('(logging) {}-0, i: {}'.format(job, iteration) in line
                for line in list_to_assert))
        self.assertTrue(
            any('(print) {}-0, i: {}'.format(job, iteration) in line
                for line in list_to_assert))

    for job in ['worker', 'ps']:
      for iteration in range(5):
        for task in range(3):
          self.assertTrue(
              any('(logging) {}-{}, i: {}'.format(job, task, iteration) in line
                  for line in list_to_assert))
          self.assertTrue(
              any('(print) {}-{}, i: {}'.format(job, task, iteration) in line
                  for line in list_to_assert))
        task = 3
        self.assertFalse(
            any('(logging) {}-{}, i: {}'.format(job, task, iteration) in line
                for line in list_to_assert))
        self.assertFalse(
            any('(print) {}-{}, i: {}'.format(job, task, iteration) in line
                for line in list_to_assert))

  def test_start_in_process_as(self):

    def proc_func():
      for i in range(5):
        logging.info('%s-%d, i: %d', multi_worker_test_base.get_task_type(),
                     self._worker_idx(), i)
        time.sleep(1)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(
            has_chief=True, num_workers=1),
        list_stdout=True)

    def eval_func():
      time.sleep(1)
      mpr.start_single_process(task_type='evaluator', task_id=0)

    eval_thread = threading.Thread(target=eval_func)
    eval_thread.start()
    mpr.start_in_process_as(as_task_type='chief', as_task_id=0)
    eval_thread.join()
    list_to_assert = mpr.join().stdout
    for job in ['worker', 'evaluator']:
      for iteration in range(5):
        self.assertTrue(
            any('{}-0, i: {}'.format(job, iteration) in line
                for line in list_to_assert))

  def test_terminate_all_does_not_ignore_error(self):
    mpr = multi_process_runner.MultiProcessRunner(
        proc_func_that_errors,
        multi_worker_test_base.create_cluster_spec(num_workers=2),
        list_stdout=True)
    mpr.start()
    time.sleep(60)
    mpr.terminate_all()
    with self.assertRaisesRegex(ValueError, 'This is an error.'):
      mpr.join()

  def test_barrier(self):
    multi_process_runner.run(
        proc_func_with_barrier,
        cluster_spec=multi_worker_test_base.create_cluster_spec(
            has_chief=True, num_workers=1),
    )

  def test_barrier_called_in_main_process(self):
    with self.assertRaises(ValueError):
      multi_process_runner.barrier()

  def test_stdout_available_when_timeout(self):

    def proc_func():
      logging.info('something printed')
      time.sleep(10000)  # Intentionally make the test timeout.

    with self.assertRaises(multi_process_runner.SubprocessTimeoutError) as cm:
      mpr = multi_process_runner.MultiProcessRunner(
          proc_func,
          multi_worker_test_base.create_cluster_spec(num_workers=1),
          list_stdout=True)
      mpr.start()
      mpr.join(timeout=60)
    mpr.terminate_all()

    list_to_assert = cm.exception.mpr_result.stdout
    self.assertTrue(
        any('something printed' in line for line in list_to_assert))

  def test_seg_fault_raises_error(self):

    def proc_func_expected_to_seg_fault():
      ctypes.string_at(0)  # Intentionally made seg fault.

    with self.assertRaises(
        multi_process_runner.UnexpectedSubprocessExitError) as cm:
      multi_process_runner.run(
          proc_func_expected_to_seg_fault,
          multi_worker_test_base.create_cluster_spec(num_workers=1),
          list_stdout=True)
    self.assertIn('Subprocess worker-0 exited with exit code',
                  str(cm.exception))
    list_to_assert = cm.exception.mpr_result.stdout
    self.assertTrue(any('SIGSEGV' in line for line in list_to_assert))

  def test_seg_fault_in_chief_raises_error(self):

    def proc_func_expected_to_seg_fault():
      if multi_worker_test_base.get_task_type() == 'worker':
        time.sleep(10000)
      ctypes.string_at(0)  # Intentionally made seg fault.

    with self.assertRaises(
        multi_process_runner.UnexpectedSubprocessExitError) as cm:
      multi_process_runner.run(
          proc_func_expected_to_seg_fault,
          multi_worker_test_base.create_cluster_spec(
              has_chief=True, num_workers=1),
          list_stdout=True)
    self.assertIn('Subprocess chief-0 exited with exit code',
                  str(cm.exception))
    list_to_assert = cm.exception.mpr_result.stdout
    self.assertTrue(any('SIGSEGV' in line for line in list_to_assert))

  def test_exit_code_is_reported_by_chief_subprocess(self):

    def proc_func_expected_to_exit_with_20():
      if multi_worker_test_base.get_task_type() == 'worker':
        time.sleep(10000)
      sys.exit(20)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func_expected_to_exit_with_20,
        multi_worker_test_base.create_cluster_spec(
            has_chief=True, num_workers=1))
    mpr.start()

    with self.assertRaisesRegex(
        multi_process_runner.UnexpectedSubprocessExitError,
        'Subprocess chief-0 exited with exit code 20'):
      mpr.join()

  def test_exit_code_is_reported_by_subprocess(self):

    def proc_func_expected_to_exit_with_10():
      sys.exit(10)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func_expected_to_exit_with_10,
        multi_worker_test_base.create_cluster_spec(num_workers=1))
    mpr.start()

    with self.assertRaisesRegex(
        multi_process_runner.UnexpectedSubprocessExitError,
        'Subprocess worker-0 exited with exit code 10'):
      mpr.join()

  def test_auto_restart(self):

    def proc_func(counter):
      counter.value += 1
      if counter.value == 1:
        raise ValueError

    manager = multi_process_runner.manager()
    counter = manager.Value(int, 0)
    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(num_workers=1),
        args=(counter,),
        auto_restart=True)
    mpr.start()
    mpr.join()
    self.assertEqual(counter.value, 2)

  def test_auto_restart_and_timeout(self):

    def proc_func():
      time.sleep(1)
      raise ValueError

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(num_workers=1),
        auto_restart=True)
    mpr.start()
    with self.assertRaises(ValueError):
      mpr.join(timeout=10)

  def test_auto_restart_and_chief(self):
    # If the chief has exited with zero exit code, auto restart should stop
    # restarting other tasks even if they fail.

    def proc_func():
      time.sleep(1)
      if multi_worker_test_base.get_task_type() != 'chief':
        raise ValueError

    manager = multi_process_runner.manager()
    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(
            has_chief=True, num_workers=1),
        auto_restart=True)
    mpr.start()
    with self.assertRaises(ValueError):
      mpr.join(timeout=10)

  def test_auto_restart_failure_immediate_after_restart(self):
    # Test the case when worker-0 fails immediately after worker-1 restarts.

    def proc_func():
      time.sleep(5)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(
            has_chief=False, num_workers=2),
        auto_restart=True)
    mpr.start()
    pid = mpr.get_process_id('worker', 1)
    mpr.terminate('worker', 1)
    while mpr.get_process_id('worker', 1) == pid:
      time.sleep(0.1)
    mpr.terminate('worker', 0)
    mpr.join(timeout=20)

  def test_auto_restart_terminate(self):
    # Tasks terminated by the user should also be restarted.

    def proc_func(counter):
      counter.value += 1
      if counter.value == 1:
        time.sleep(100)

    manager = multi_process_runner.manager()
    counter = manager.Value(int, 0)

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(
            has_chief=False, num_workers=1),
        args=(counter,),
        auto_restart=True)
    mpr.start()
    time.sleep(3)
    mpr.terminate('worker', 0)
    mpr.join(timeout=20)
    self.assertEqual(counter.value, 2)

  def test_error_reporting_overrides_timeout_reporting(self):

    def proc_func():
      if self._worker_idx() == 1:
        time.sleep(10000)
      raise ValueError('Worker 0 errored')

    mpr = multi_process_runner.MultiProcessRunner(
        proc_func,
        multi_worker_test_base.create_cluster_spec(num_workers=2))
    mpr.start()

    with self.assertRaisesRegex(
        ValueError,
        'Worker 0 errored'):
      mpr.join(timeout=20)


class MultiProcessPoolRunnerTest(test.TestCase):

  def test_same_process_across_runs(self):
    cluster_spec = multi_worker_test_base.create_cluster_spec(num_workers=2)
    runner = multi_process_runner.MultiProcessPoolRunner(cluster_spec)
    pid = runner.run(proc_func_that_returns_pid)
    for _ in range(3):
      self.assertAllEqual(runner.run(proc_func_that_returns_pid), pid)

  def test_exceptions_in_sub_process(self):
    cluster_spec = multi_worker_test_base.create_cluster_spec(num_workers=2)
    runner = multi_process_runner.MultiProcessPoolRunner(cluster_spec)
    pid = runner.run(proc_func_that_returns_pid)
    with self.assertRaisesRegex(ValueError, 'This is an error.'):
      runner.run(proc_func_that_errors)
    self.assertAllEqual(runner.run(proc_func_that_returns_pid), pid)

  def test_tf_config(self):
    cluster_spec = multi_worker_test_base.create_cluster_spec(
        has_chief=True, num_workers=2)
    runner = multi_process_runner.MultiProcessPoolRunner(cluster_spec)
    result = runner.run(proc_func_that_adds_task_type_in_return_data)

    job_count_dict = {'worker': 2, 'chief': 1}
    for data in result:
      job_count_dict[data] -= 1

    self.assertEqual(job_count_dict['worker'], 0)
    self.assertEqual(job_count_dict['chief'], 0)

  @unittest.expectedFailure
  def test_exception_in_main_process(self):
    # When there's an exception in the main process, __del__() is not called.
    # This test is to verify MultiProcessPoolRunner can cope with __del__() not
    # being called.
    cluster_spec = multi_worker_test_base.create_cluster_spec(
        has_chief=True, num_workers=2)
    runner = multi_process_runner.MultiProcessPoolRunner(cluster_spec)
    runner.run(proc_func_that_returns_pid)
    raise ValueError('failure')

  def test_initializer(self):
    cluster_spec = multi_worker_test_base.create_cluster_spec(num_workers=2)
    runner = multi_process_runner.MultiProcessPoolRunner(
        cluster_spec, initializer=lambda: proc_func_that_sets_global(1))
    result = runner.run(proc_func_that_sets_global, args=(2,))
    self.assertAllEqual(result, [1, 1])


if __name__ == '__main__':
  multi_process_runner.test_main()
