# Copyright 2016 Google Inc.  All Rights Reserved.
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

"""Classes representing the various compiler and linter tools that are used to
build Shaka Player."""

import json
import logging
import os
import re
import shutil
import subprocess

import shakaBuildHelpers


def _canonicalize_source_files(source_files):
  """Canonicalize a set or list of source files.

  This makes all path names cygwin-safe and sorted."""

  files = [shakaBuildHelpers.cygwin_safe_path(f) for f in source_files]
  files.sort()
  return files

def _get_source_path(path):
  """Take path components of a source file as arguments and return an absolute,
     cygwin-safe path."""
  return shakaBuildHelpers.cygwin_safe_path(os.path.join(
      shakaBuildHelpers.get_source_base(), path))

def _must_build(output, source_files):
  """Returns True if any of the |source_files| have changed since |output| was
     built, or if |output| does not exist yet."""
  if not os.path.isfile(output):
    # Nothing built, so we should build the output.
    return True

  # Detect changes to the set of files that we intend to build.
  build_time = os.path.getmtime(output)
  # See if any files were modified since the output was created.
  if any(os.path.getmtime(f) > build_time for f in source_files):
    # Some input files have changed, so we should build again.
    return True

  logging.warning('No changes detected, skipping. Use --force to override.')
  return False

def _update_timestamp(path):
  # This creates the file if it does not exist, and updates the timestamp if it
  # does.
  open(path, 'w').close()


class ClosureCompiler(object):
  def __init__(self, source_files, build_name):
    self.source_files = _canonicalize_source_files(source_files)

    prefix = _get_source_path('dist/' + build_name)
    self.compiled_js_path = prefix + '.js'
    self.source_map_path = prefix + '.map'

    # These can be overridden for special cases:

    # If True, output the compiled bundle to a file.
    self.output_compiled_bundle = True
    # If True, generate a source map and attach it to the output bundle.
    self.add_source_map = True
    # If True, wrap the output in a wrapper that prevents window pollution.
    self.add_wrapper = True
    # If not None, use a timestamp file for change detection, and touch that
    # timestamp file after compilation.
    self.timestamp_file = None

  def compile(self, options, force=False):
    """Builds the files in |self.source_files| using the given Closure
    command-line options.

    Args:
      options: An array of options to give to Closure.
      force: Generate the output even if the inputs have not changed.

    Returns:
      True on success; False on failure.
    """
    if not force:
      if self.timestamp_file:
        if not _must_build(self.timestamp_file, self.source_files):
          return True
      else:
        if not _must_build(self.compiled_js_path, self.source_files):
          return True

    jar = _get_source_path('third_party/closure/compiler.jar')

    output_options = []
    if self.output_compiled_bundle:
      output_options += [
          '--js_output_file', self.compiled_js_path,
      ]

      if self.add_source_map:
        source_base = _get_source_path('')

        # The source map below would be silently broken if the format of
        # source_base changed without updating the source_map_location_mapping
        # argument below.  This assertion ensures that this doesn't happen.
        assert source_base[-1] == '/', 'Source base format changed!'

        output_options += [
            '--create_source_map', self.source_map_path,
            '--source_map_location_mapping', source_base + '|../',
        ]

      if self.add_wrapper:
        output_options += self._prepare_wrapper()

    cmd_line = ['java', '-jar', jar] + output_options + options
    cmd_line += self.source_files

    if shakaBuildHelpers.execute_get_code(cmd_line) != 0:
      logging.error('Build failed')
      return False

    if self.output_compiled_bundle and self.add_source_map:
      # Add a special source-mapping comment so that Chrome and Firefox can map
      # line and character numbers from the compiled library back to the
      # original source locations.
      with open(self.compiled_js_path, 'a') as f:
        f.write('//# sourceMappingURL=%s' % os.path.basename(
            self.source_map_path))

    if self.timestamp_file:
      _update_timestamp(self.timestamp_file)

    return True

  def _prepare_wrapper(self):
    """Prepares an output wrapper and returns a list of command line arguments
       for Closure Compiler to use it."""

    # Load the wrapper and use Closure to strip whitespace and comments.
    # This requires %output% in the template to be protected, so Closure doesn't
    # fail to parse it.
    wrapper_input_path = _get_source_path('build/wrapper.template.js')
    wrapper_output_path = _get_source_path('dist/wrapper.js')

    with open(wrapper_input_path, 'rb') as f:
      wrapper_code = f.read().decode('utf8').replace('%output%', '"%output%"')

    jar = _get_source_path('third_party/closure/compiler.jar')
    cmd_line = ['java', '-jar', jar, '-O', 'WHITESPACE_ONLY']

    proc = shakaBuildHelpers.execute_subprocess(
        cmd_line, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    stripped_wrapper_code = proc.communicate(wrapper_code.encode('utf8'))[0]

    if proc.returncode != 0:
      raise RuntimeError('Failed to strip whitespace from wrapper!')

    with open(wrapper_output_path, 'wb') as f:
      code = stripped_wrapper_code.decode('utf8')
      f.write(code.replace('"%output%"', '%output%').encode('utf8'))

    return ['--output_wrapper_file=%s' % wrapper_output_path]


class ExternGenerator(object):
  def __init__(self, source_files, build_name):
    self.source_files = _canonicalize_source_files(source_files)
    self.output = _get_source_path('dist/' + build_name + '.externs.js')

  def generate(self, force=False):
    """Generates externs for the files in |self.source_files|.

    Args:
      force: Generate the output even if the inputs have not changed.

    Returns:
      True on success; False on failure.
    """
    if not force and not _must_build(self.output, self.source_files):
      return True

    extern_generator = _get_source_path('build/generateExterns.js')

    cmd_line = ['node', extern_generator, '--output', self.output]
    cmd_line += self.source_files

    if shakaBuildHelpers.execute_get_code(cmd_line) != 0:
      logging.error('Externs generation failed')
      return False

    return True


class Less(object):
  def __init__(self, source_files, output):
    self.source_files = _canonicalize_source_files(source_files)
    self.output = output

  def compile(self, force=False):
    """Compiles the less files in |self.source_files| into the |self.output|
       css file.

    Args:
      force: Generate the output even if the inputs have not changed.

    Returns:
      True on success; False on failure.
    """
    if not force and not _must_build(self.output, self.source_files):
      return True

    lessc = shakaBuildHelpers.get_node_binary('less', 'lessc')

    cmd_line = lessc + self.source_files + [self.output]

    if shakaBuildHelpers.execute_get_code(cmd_line) != 0:
      logging.error('Externs generation failed')
      return False

    return True


class Linter(object):
  def __init__(self, source_files, config_path):
    self.source_files = _canonicalize_source_files(source_files)
    self.config_path = config_path
    self.output = _get_source_path('dist/.lintstamp')

  def lint(self, fix=False, force=False):
    """Run linter checks on the files in |self.source_files|.

    Args:
      fix: If True, ask the linter to fix what errors it can automatically.
      force: Run linter checks even if the inputs have not changed.

    Returns:
      True on success; False on failure.
    """
    deps = self.source_files + [self.config_path]
    if not force and not _must_build(self.output, deps):
      return True

    eslint = shakaBuildHelpers.get_node_binary('eslint')
    cmd_line = eslint + ['--config', self.config_path] + self.source_files

    if fix:
      cmd_line += ['--fix']

    if shakaBuildHelpers.execute_get_code(cmd_line) != 0:
      return False

    # TODO: Add back Closure Compiler Linter

    # Update the timestamp of the file that tracks when we last updated.
    _update_timestamp(self.output)
    return True


class HtmlLinter(object):
  def __init__(self, source_files, config_path):
    self.source_files = _canonicalize_source_files(source_files)
    self.config_path = config_path
    self.output = _get_source_path('dist/.htmllintstamp')

  def lint(self, force=False):
    """Run HTML linter checks on the files in |self.source_files|.

    Args:
      force: Run linter checks even if the inputs have not changed.

    Returns:
      True on success; False on failure.
    """
    deps = self.source_files + [self.config_path]
    if not force and not _must_build(self.output, deps):
      return True

    htmlhint = shakaBuildHelpers.get_node_binary('htmlhint')
    cmd_line = htmlhint + ['--config=' + self.config_path] + self.source_files

    if shakaBuildHelpers.execute_get_code(cmd_line) != 0:
      return False

    # Update the timestamp of the file that tracks when we last updated.
    _update_timestamp(self.output)
    return True


class Jsdoc(object):
  def __init__(self, config_path):
    self.config_path = config_path
    self.source_files = []

    # Just one of many output files, used to check the freshness of the docs.
    self.output = _get_source_path('docs/api/index.html')

    # To avoid getting out of sync with the source files jsdoc actually reads,
    # parse the config file and locate all source files based on that.
    match = re.compile(r'.*\.js$')
    with open(self.config_path, 'rb') as f:
      config = json.load(f)
    for path in config['source']['include']:
      full_path = _get_source_path(path)
      self.source_files += shakaBuildHelpers.get_all_files(full_path, match)

  def build(self, force=False):
    """Build the documentation.

    Args:
      force: Build the docs even if the inputs have not changed.

    Returns:
      True on success; False on failure.
    """
    deps = self.source_files + [self.config_path]
    if not force and not _must_build(self.output, deps):
      return True

    base = _get_source_path('')

    # Wipe out any old docs.
    shutil.rmtree(os.path.join(base, 'docs', 'api'), ignore_errors=True)

    # Jsdoc expects to run from the base dir.
    with shakaBuildHelpers.InDir(base):
      jsdoc = shakaBuildHelpers.get_node_binary('jsdoc')
      cmd_line = jsdoc + ['-c', self.config_path]
      if shakaBuildHelpers.execute_get_code(cmd_line) != 0:
        return False

    return True
