# Lint as: python2, python3
# Copyright 2019 Google LLC. All Rights Reserved.
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
"""Chicago taxi example using TFX."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from typing import List, Text

import absl
import tensorflow_model_analysis as tfma
from tfx.components import CsvExampleGen
from tfx.components import Evaluator
from tfx.components import ExampleValidator
from tfx.components import ImporterNode
from tfx.components import Pusher
from tfx.components import ResolverNode
from tfx.components import SchemaGen
from tfx.components import StatisticsGen
from tfx.components import Trainer
from tfx.components import Transform
from tfx.dsl.experimental import latest_blessed_model_resolver
from tfx.orchestration import metadata
from tfx.orchestration import pipeline
from tfx.orchestration.beam.beam_dag_runner import BeamDagRunner
from tfx.proto import pusher_pb2
from tfx.proto import trainer_pb2
from tfx.types import Channel
from tfx.types.standard_artifacts import Model
from tfx.types.standard_artifacts import ModelBlessing
from tfx.types.standard_artifacts import Schema
from tfx_bsl.version import __version__ as tfx_bsl_version

_pipeline_name = 'chicago_taxi_importer'

# This example assumes that the taxi data is stored in ~/taxi/data and the
# taxi utility function is in ~/taxi.  Feel free to customize this as needed.
_taxi_root = os.path.join(os.environ['HOME'], 'taxi')
_data_root = os.path.join(_taxi_root, 'data', 'simple')
# Python module file to inject customized logic into the TFX components. The
# Transform and Trainer both require user-defined functions to run successfully.
_module_file = os.path.join(_taxi_root, 'taxi_utils.py')
# Path which can be listened to by the model server.  Pusher will output the
# trained model here.
_serving_model_dir = os.path.join(_taxi_root, 'serving_model', _pipeline_name)

# Directory and data locations.  This example assumes all of the chicago taxi
# example code and metadata library is relative to $HOME, but you can store
# these files anywhere on your local filesystem.
_tfx_root = os.path.join(os.environ['HOME'], 'tfx')
_pipeline_root = os.path.join(_tfx_root, 'pipelines', _pipeline_name)
# Sqlite ML-metadata db path.
_metadata_path = os.path.join(_tfx_root, 'metadata', _pipeline_name,
                              'metadata.db')
_user_schema_path = os.path.join(_taxi_root, 'data', 'user_provided_schema')

# Pipeline arguments for Beam powered Components.
_beam_pipeline_args = [
    '--direct_running_mode=multi_processing',
    # 0 means auto-detect based on on the number of CPUs available
    # during execution time.
    '--direct_num_workers=0',
]


def _create_pipeline(pipeline_name: Text, pipeline_root: Text, data_root: Text,
                     user_schema_path: Text, module_file: Text,
                     serving_model_dir: Text, metadata_path: Text,
                     beam_pipeline_args: List[Text]) -> pipeline.Pipeline:
  """Implements the chicago taxi pipeline with TFX."""
  # Brings data into the pipeline or otherwise joins/converts training data.
  example_gen = CsvExampleGen(input_base=data_root)

  # Computes statistics over data for visualization and example validation.
  statistics_gen = StatisticsGen(examples=example_gen.outputs['examples'])

  # Import user-provided schema.
  user_schema_importer = ImporterNode(
      instance_name='import_user_schema',
      source_uri=user_schema_path,
      artifact_type=Schema)

  # Generates schema based on statistics files. Even we use user-provided schema
  # in downstream components, we still want to generate the schema of the newest
  # data so that user can compare and optionally update the schema to use.
  schema_gen = SchemaGen(
      statistics=statistics_gen.outputs['statistics'],
      infer_feature_shape=False)

  # Performs anomaly detection based on statistics and data schema.
  example_validator = ExampleValidator(
      statistics=statistics_gen.outputs['statistics'],
      schema=user_schema_importer.outputs['result'])

  # Performs transformations and feature engineering in training and serving.
  transform = Transform(
      examples=example_gen.outputs['examples'],
      schema=user_schema_importer.outputs['result'],
      module_file=module_file)

  # Uses user-provided Python function that implements a model using TF-Learn.
  trainer = Trainer(
      module_file=module_file,
      transformed_examples=transform.outputs['transformed_examples'],
      schema=user_schema_importer.outputs['result'],
      transform_graph=transform.outputs['transform_graph'],
      train_args=trainer_pb2.TrainArgs(num_steps=10000),
      eval_args=trainer_pb2.EvalArgs(num_steps=5000))

  # Get the latest blessed model for model validation.
  model_resolver = ResolverNode(
      instance_name='latest_blessed_model_resolver',
      resolver_class=latest_blessed_model_resolver.LatestBlessedModelResolver,
      model=Channel(type=Model),
      model_blessing=Channel(type=ModelBlessing))

  # Uses TFMA to compute a evaluation statistics over features of a model and
  # perform quality validation of a candidate model (compared to a baseline).
  eval_config = tfma.EvalConfig(
      model_specs=[tfma.ModelSpec(signature_name='eval')],
      slicing_specs=[
          tfma.SlicingSpec(),
          tfma.SlicingSpec(feature_keys=['trip_start_hour'])
      ],
      metrics_specs=[
          tfma.MetricsSpec(
              thresholds={
                  'accuracy':
                      tfma.config.MetricThreshold(
                          value_threshold=tfma.GenericValueThreshold(
                              lower_bound={'value': 0.6}),
                          change_threshold=tfma.GenericChangeThreshold(
                              direction=tfma.MetricDirection.HIGHER_IS_BETTER,
                              absolute={'value': -1e-10}))
              })
      ])
  evaluator = Evaluator(
      examples=example_gen.outputs['examples'],
      model=trainer.outputs['model'],
      baseline_model=model_resolver.outputs['model'],
      # Change threshold will be ignored if there is no baseline (first run).
      eval_config=eval_config)

  # Checks whether the model passed the validation steps and pushes the model
  # to a file destination if check passed.
  pusher = Pusher(
      model=trainer.outputs['model'],
      model_blessing=evaluator.outputs['blessing'],
      push_destination=pusher_pb2.PushDestination(
          filesystem=pusher_pb2.PushDestination.Filesystem(
              base_directory=serving_model_dir)))

  return pipeline.Pipeline(
      pipeline_name=pipeline_name,
      pipeline_root=pipeline_root,
      components=[
          example_gen, statistics_gen, user_schema_importer, schema_gen,
          example_validator, transform, trainer, model_resolver, evaluator,
          pusher
      ],
      enable_cache=True,
      metadata_connection_config=metadata.sqlite_metadata_connection_config(
          metadata_path),
      beam_pipeline_args=beam_pipeline_args)


# To run this pipeline from the python CLI:
#   $python taxi_pipeline_beam.py
if __name__ == '__main__':
  absl.logging.set_verbosity(absl.logging.INFO)

  BeamDagRunner().run(
      _create_pipeline(
          pipeline_name=_pipeline_name,
          pipeline_root=_pipeline_root,
          data_root=_data_root,
          user_schema_path=_user_schema_path,
          module_file=_module_file,
          serving_model_dir=_serving_model_dir,
          metadata_path=_metadata_path,
          beam_pipeline_args=_beam_pipeline_args))
