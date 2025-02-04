# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
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
# =============================================================================
"""Library that generates the API for tensorflow."""

import collections
from collections.abc import Mapping, MutableMapping, Sequence
import dataclasses
import os
from typing import Optional, cast

from tensorflow.python.tools.api.generator2.shared import exported_api

_GENERATED_FILE_HEADER = """# This file is MACHINE GENERATED! Do not edit.
# Generated by: tensorflow/python/tools/api/generator2/generator/generator.py script.
\"\"\"%s
\"\"\"

import sys as _sys

"""

_LAZY_LOADING_MODULE_TEXT_TEMPLATE = """
# Inform pytype that this module is dynamically populated (b/111239204).
_HAS_DYNAMIC_ATTRIBUTES = True
_PUBLIC_APIS = {
%s
}
"""
_DEPRECATION_FOOTER = """
from tensorflow.python.util import module_wrapper as _module_wrapper

if not isinstance(_sys.modules[__name__], _module_wrapper.TFModuleWrapper):
  _sys.modules[__name__] = _module_wrapper.TFModuleWrapper(
      _sys.modules[__name__], "%s", public_apis=%s, deprecation=%s,
      has_lite=%s)
"""


class DocExportedTwiceError(Exception):
  """Exception for when two docstrings are registered to a single module."""


def _get_import_path(
    file: str, file_prefixes_to_strip: Sequence[str], module_prefix: str
) -> str:
  module_import_path = file
  for prefix in file_prefixes_to_strip:
    module_import_path = module_import_path.removeprefix(prefix)
  module_import_path = module_import_path.removesuffix('.py')
  module_import_path = module_import_path.removesuffix('__init__')
  module_import_path = module_import_path.strip('/')
  module_import_path = module_import_path.replace('/', '.')

  return module_prefix + module_import_path


@dataclasses.dataclass(frozen=True)
class _Entrypoint:
  """An entrypoint that was exposed by the use of a decorator.

  Attributes:
    module: The public module that the symbol was exposed to. For example:
      tensorflow.io.
    name: The name the symbol was exported as. For example: decode_png.
    exported_symbol: The symbol that this entrypoint refers back to.
  """

  module: str
  name: str
  exported_symbol: exported_api.ExportedSymbol

  def get_import(
      self,
      file_prefixes_to_strip: Sequence[str],
      module_prefix: str,
      use_lazy_loading: bool,
  ) -> str:
    """Returns the import statement for this entrypoint.

    Args:
      file_prefixes_to_strip: List of prefixes to strip from the file name.
      module_prefix: A prefix to add to the import.
      use_lazy_loading: Whether to use lazy loading or not.
    """
    module_import_path = _get_import_path(
        self.exported_symbol.file_name, file_prefixes_to_strip, module_prefix
    )
    alias = ''
    symbol_name = self.exported_symbol.symbol_name
    if self.name != symbol_name:
      alias = f' as {self.name}'
    if not use_lazy_loading:
      return (
          f'from {module_import_path} import'
          f' {symbol_name}{alias} # line:'
          f' {self.exported_symbol.line_no}'
      )
    else:
      return (
          f"  '{self.name}': ('{module_import_path}',"
          f" '{symbol_name}'), # line:"
          f' {self.exported_symbol.line_no}'
      )


@dataclasses.dataclass(frozen=True)
class PublicAPI:
  v1_entrypoints_by_module: Mapping[str, set[_Entrypoint]]
  v2_entrypoints_by_module: Mapping[str, set[_Entrypoint]]
  v1_generated_imports_by_module: Mapping[str, set[str]]
  v2_generated_imports_by_module: Mapping[str, set[str]]
  docs_by_module: Mapping[str, str]


def get_module(dir_path: str, relative_to_dir: str) -> str:
  """Get module that corresponds to path relative to relative_to_dir.

  Args:
    dir_path: Path to directory.
    relative_to_dir: Get module relative to this directory.

  Returns:
    Name of module that corresponds to the given directory.
  """
  dir_path = dir_path[len(relative_to_dir) :]
  # Convert path separators to '/' for easier parsing below.
  dir_path = dir_path.replace(os.sep, '/')
  return dir_path.replace('/', '.').strip('.')


def generate_proxy_api_files(
    output_files: list[str], proxy_module_root: str, output_dir: str
):
  """Creates __init__.py files in proxy format for the Python API.

  Args:
    output_files: List of __init__.py file paths to create.
    proxy_module_root: Module root for proxy-import format. If specified, proxy
      files with content like `from proxy_module_root.proxy_module import *`
      will be created to enable import resolution under TensorFlow.
    output_dir: output API root directory.
  """
  for file in output_files:
    file_dir = os.path.dirname(file)
    if not os.path.isdir(file_dir):
      os.makedirs(file_dir)
    module = get_module(file_dir, output_dir)
    content = f'from {proxy_module_root}.{module} import *'
    with open(file, 'w') as f:
      f.write(content)


def _should_skip_file(
    file: str,
    file_prefixes_to_strip: Sequence[str],
    packages_to_ignore: Sequence[str],
    module_prefix: str,
) -> bool:
  import_path = _get_import_path(file, file_prefixes_to_strip, module_prefix)
  return any(import_path.startswith(package) for package in packages_to_ignore)


def get_public_api(
    api_mapping_files: Sequence[str],
    file_prefixes_to_strip: Sequence[str],
    packages_to_ignore: Sequence[str],
    output_package: str,
    module_prefix: str,
) -> PublicAPI:
  """Generates the structure of the public API from the given files.

  Args:
    api_mapping_files: List of files containing the exported API mappings and
      docstrings.
    file_prefixes_to_strip: A list of prefixes to strip from files when
      determining the packages to ignore.
    packages_to_ignore: A list of python packages that should be ignored when
      searching for tf_exports.
    output_package: The package to use for the imports.
    module_prefix: A prefix to add to the non-generated imports.

  Raises:
    DocExportedTwiceError: Two docstrings are registered for the same module.

  Returns:
    The public API structure.
  """
  ea = exported_api.ExportedApi()
  for f in api_mapping_files:
    ea.read(f)

  v1_entrypoints_by_module = collections.defaultdict(set)
  v2_entrypoints_by_module = collections.defaultdict(set)

  def add_exported_symbols(
      api_names: list[str],
      s: exported_api.ExportedSymbol,
      entrypoints_by_module: Mapping[str, set[_Entrypoint]],
  ):
    for api_name in api_names:
      index_of_last_dot = api_name.rfind('.')
      index_of_first_dot = api_name.find('.')
      module = output_package
      if index_of_first_dot + 1 < index_of_last_dot:
        module += f'.{api_name[index_of_first_dot + 1:index_of_last_dot]}'
      name = api_name[index_of_last_dot + 1 :]
      entrypoints_by_module[module].add(_Entrypoint(module, name, s))

  for s in ea.symbols:
    if _should_skip_file(
        s.file_name, file_prefixes_to_strip, packages_to_ignore, module_prefix
    ):
      continue
    add_exported_symbols(s.v1_apis, s, v1_entrypoints_by_module)
    add_exported_symbols(s.v2_apis, s, v2_entrypoints_by_module)

  v1_generated_imports_by_module = collections.defaultdict(set)
  v2_generated_imports_by_module = collections.defaultdict(set)

  def add_generated_imports(
      entrypoints_by_module: Mapping[str, set[_Entrypoint]],
      generated_imports_by_module: Mapping[str, set[str]],
  ):
    for module in entrypoints_by_module:
      i = module.rfind('.')
      if i == -1:
        continue
      while i != -1:
        parent = module[:i]
        generated_imports_by_module[parent].add(module)
        module = parent
        i = module.rfind('.')

  add_generated_imports(
      v1_entrypoints_by_module, v1_generated_imports_by_module
  )
  add_generated_imports(
      v2_entrypoints_by_module, v2_generated_imports_by_module
  )

  docs_by_module = {}

  for d in ea.docs:
    for m in d.modules:
      if m in docs_by_module:
        raise DocExportedTwiceError(
            f'Docstring at {d.file_name}:{d.line_no} is registered for {m},'
            ' which already has a registered docstring.'
        )
      docs_by_module[m] = d.docstring

  return PublicAPI(
      v1_entrypoints_by_module=v1_entrypoints_by_module,
      v2_entrypoints_by_module=v2_entrypoints_by_module,
      v1_generated_imports_by_module=v1_generated_imports_by_module,
      v2_generated_imports_by_module=v2_generated_imports_by_module,
      docs_by_module=docs_by_module,
  )


def _get_module_docstring(
    docs_by_module: Mapping[str, str], module: str
) -> str:
  if module in docs_by_module:
    return docs_by_module[module]
  module = module.replace('tensorflow', 'tf')
  return f'Public API for {module} namespace'


def _get_imports_for_module(
    module: str,
    output_package: str,
    symbols_by_module: Mapping[str, set[_Entrypoint]],
    generated_imports_by_module: Mapping[str, set[str]],
    file_prefixes_to_strip: Sequence[str],
    module_prefix: str,
    use_lazy_loading: bool,
    subpackage_rewrite: Optional[str],
) -> str:
  """Returns the imports for a module.

  Args:
    module: The module to get imports for.
    output_package: The package to use for the imports.
    symbols_by_module: The symbols that should be exposed by each module.
    generated_imports_by_module: The sub-modules that should be exposed by each
      module.
    file_prefixes_to_strip: The prefixes to strip from the file names of the
      imports.
    module_prefix: A prefix to add to the non-generated imports.
    use_lazy_loading: Whether to use lazy loading or not.
    subpackage_rewrite: The subpackage to use for the imports.
  """
  content = ''
  symbol_imports = list(symbols_by_module[module])
  symbol_imports = sorted(
      symbol_imports, key=lambda s: s.exported_symbol.file_name
  )
  for imp in generated_imports_by_module[module]:
    if subpackage_rewrite:
      imp = imp.replace(output_package, subpackage_rewrite)
    last_dot = imp.rfind('.')
    if use_lazy_loading:
      content += f"  '{imp[last_dot+1:]}': ('', '{imp}'),\n"
    else:
      content += f'from {imp[:last_dot]} import {imp[last_dot+1:]}\n'
  for s in symbol_imports:
    content += (
        f'{s.get_import(file_prefixes_to_strip, module_prefix, use_lazy_loading=use_lazy_loading)}\n'
    )
  return content


def gen_public_api(
    output_dir: str,
    output_package: str,
    root_init_template: str,
    api_version: int,
    compat_api_versions: Sequence[int],
    compat_init_templates: Sequence[str],
    use_lazy_loading: bool,
    file_prefixes_to_strip: Sequence[str],
    mapping_files: Sequence[str],
    packages_to_ignore: Sequence[str],
    module_prefix: str,
    root_file_name: str,
):
  """Generates the public API for tensorflow.

  Args:
    output_dir: The directory to output the files to.
    output_package: The package to use for the imports.
    root_init_template: The template for the root init file.
    api_version: The version of the API to generate.
    compat_api_versions: The versions of the compat APIs to generate.
    compat_init_templates: The templates for the compat init files.
    use_lazy_loading: Whether to use lazy loading or not.
    file_prefixes_to_strip: The prefixes to strip from the file names of the
      imports.
    mapping_files: The mapping files created by the API Extractor.
    packages_to_ignore: A list of python packages that should be ignored when
      searching for tf_exports.
    module_prefix: A prefix to add to the non-generated imports.
    root_file_name: The file name that should be generated for the top level
      API.
  """
  public_api = get_public_api(
      mapping_files,
      file_prefixes_to_strip,
      packages_to_ignore,
      output_package,
      module_prefix,
  )

  root_entrypoints_by_module = public_api.v2_entrypoints_by_module
  root_generated_imports_by_module = public_api.v2_generated_imports_by_module
  if api_version == 1:
    root_entrypoints_by_module = public_api.v1_entrypoints_by_module
    root_generated_imports_by_module = public_api.v1_generated_imports_by_module

  for compat_version in compat_api_versions:
    compat_package = f'{output_package}.compat'
    compat_version_package = f'{compat_package}.v{compat_version}'
    public_api.v2_generated_imports_by_module[compat_package].add(
        compat_version_package
    )
    public_api.v1_generated_imports_by_module[compat_package].add(
        compat_version_package
    )

  _gen_init_files(
      output_dir,
      output_package,
      api_version,
      root_entrypoints_by_module,
      root_generated_imports_by_module,
      public_api.docs_by_module,
      root_init_template,
      file_prefixes_to_strip,
      use_lazy_loading,
      module_prefix,
      root_file_name=root_file_name,
  )

  for compat_index, compat_version in enumerate(compat_api_versions):
    compat_output_dir = os.path.join(output_dir, 'compat', f'v{compat_version}')
    os.makedirs(compat_output_dir, exist_ok=True)
    compat_version = int(compat_version)

    compat_entrypoints_by_module = public_api.v2_entrypoints_by_module
    compat_generated_imports_by_module = (
        public_api.v2_generated_imports_by_module
    )
    if compat_version == 1:
      compat_entrypoints_by_module = public_api.v1_entrypoints_by_module
      compat_generated_imports_by_module = (
          public_api.v1_generated_imports_by_module
      )

    _gen_init_files(
        compat_output_dir,
        output_package,
        compat_version,
        compat_entrypoints_by_module,
        compat_generated_imports_by_module,
        public_api.docs_by_module,
        compat_init_templates[compat_index] if compat_init_templates else '',
        file_prefixes_to_strip,
        use_lazy_loading,
        module_prefix,
        subpackage_rewrite=f'{output_package}.compat.v{compat_version}',
    )

    for nested_compat_index, nested_compat_version in enumerate(
        compat_api_versions
    ):
      nested_compat_version = int(nested_compat_version)
      nested_compat_output_dir = os.path.join(
          compat_output_dir, 'compat', f'v{nested_compat_version}'
      )
      nested_compat_entrypoints_by_module = public_api.v2_entrypoints_by_module
      nested_compat_generated_imports_by_module = (
          public_api.v2_generated_imports_by_module
      )
      if nested_compat_version == 1:
        nested_compat_entrypoints_by_module = (
            public_api.v1_entrypoints_by_module
        )
        nested_compat_generated_imports_by_module = (
            public_api.v1_generated_imports_by_module
        )
      os.makedirs(nested_compat_output_dir, exist_ok=True)
      gen_nested_compat_files(
          nested_compat_output_dir,
          output_package,
          nested_compat_version,
          nested_compat_entrypoints_by_module,
          nested_compat_generated_imports_by_module,
          public_api.docs_by_module,
          compat_init_templates[nested_compat_index]
          if compat_init_templates
          else '',
          file_prefixes_to_strip,
          use_lazy_loading,
          compat_api_versions,
          module_prefix
      )


def _get_module_wrapper(
    module: str,
    output_dir: str,
    output_package: str,
    api_version: int,
    symbols_by_module: Mapping[str, set[_Entrypoint]],
    use_lazy_loading: bool,
) -> str:
  """Returns the module wrapper for the given module."""
  if api_version != 1 and not use_lazy_loading:
    return ''
  deprecated = 'False'
  has_lite = 'False'
  public_apis_name = 'None'
  if api_version == 1 and not output_dir.strip('/').endswith('compat/v1'):
    deprecated = 'True'
  if 'lite' in symbols_by_module and use_lazy_loading:
    has_lite = 'True'
  if use_lazy_loading:
    public_apis_name = '_PUBLIC_APIS'
  return _DEPRECATION_FOOTER % (
      module.removeprefix(output_package).strip('.'),
      public_apis_name,
      deprecated,
      has_lite,
  )


def _gen_init_files(
    output_dir: str,
    output_package: str,
    api_version: int,
    symbols_by_module: Mapping[str, set[_Entrypoint]],
    generated_imports_by_module: Mapping[str, set[str]],
    docs_by_module: Mapping[str, str],
    root_template_path: str,
    file_prefixes_to_strip: Sequence[str],
    use_lazy_loading: bool,
    module_prefix: str,
    subpackage_rewrite: Optional[str] = None,
    root_file_name='__init__.py',
):
  """Generates the __init__.py files for the given API version."""
  modules = set(symbols_by_module.keys())
  modules.update(generated_imports_by_module.keys())
  for module in modules:
    if len(module) < len(output_package):
      continue
    module_relative_to_package = module[len(output_package) + 1 :]
    module_path = os.path.join(
        output_dir, module_relative_to_package.replace('.', '/')
    )
    os.makedirs(module_path, exist_ok=True)
    module_file_path = os.path.join(
        module_path,
        root_file_name if not module_relative_to_package else '__init__.py',
    )
    with open(module_file_path, 'w') as f:
      module_imports = _get_imports_for_module(
          module,
          output_package,
          symbols_by_module,
          generated_imports_by_module,
          file_prefixes_to_strip,
          module_prefix,
          use_lazy_loading,
          subpackage_rewrite,
      )
      if use_lazy_loading:
        module_imports = _LAZY_LOADING_MODULE_TEXT_TEMPLATE % module_imports
      # If this module is the root and there is a root template, use it
      if module == output_package and root_template_path:
        with open(root_template_path, 'r') as template:
          content = template.read()
          content = content.replace('# API IMPORTS PLACEHOLDER', module_imports)

          underscore_elements = [
              s.name
              for s in symbols_by_module[module]
              if s.name.startswith('_')
          ]
          for i in generated_imports_by_module[module]:
            module_name = i[i.rfind('.') + 1 :]
            if module_name.startswith('_'):
              underscore_elements.append(module_name)

          root_module_footer = f"""
_names_with_underscore = [{', '.join(sorted([f"'{s}'" for s in underscore_elements]))}]
__all__ = [_s for _s in dir() if not _s.startswith('_')]
__all__.extend([_s for _s in _names_with_underscore])
          """

          content = content.replace('# __all__ PLACEHOLDER', root_module_footer)

          content = content.replace(
              '# WRAPPER_PLACEHOLDER',
              _get_module_wrapper(
                  module,
                  output_dir,
                  output_package,
                  api_version,
                  symbols_by_module,
                  use_lazy_loading,
              ),
          )

          f.write(content)
          continue

      f.write(
          _GENERATED_FILE_HEADER % _get_module_docstring(docs_by_module, module)
      )

      f.write(module_imports)

      f.write(
          _get_module_wrapper(
              module,
              output_dir,
              output_package,
              api_version,
              symbols_by_module,
              use_lazy_loading,
          )
      )


def gen_nested_compat_files(
    output_dir: str,
    output_package: str,
    api_version: int,
    symbols_by_module: Mapping[str, set[_Entrypoint]],
    generated_imports_by_module: Mapping[str, set[str]],
    docs_by_module: Mapping[str, str],
    root_template_path: str,
    file_prefixes_to_strip: Sequence[str],
    use_lazy_loading: bool,
    compat_versions: Sequence[int],
    module_prefix: str,
):
  """Generates the nested compat __init__.py files."""
  nested_compat_symbols_by_module = cast(
      MutableMapping[str, set[_Entrypoint]], symbols_by_module.copy()
  )
  nested_generated_imports_by_module = generated_imports_by_module.copy()
  compat_module = f'{output_package}.compat'
  modules_to_remove = [
      module
      for module in symbols_by_module.keys()
      if module != output_package and module != compat_module
  ]
  for module in modules_to_remove:
    del nested_compat_symbols_by_module[module]

  for compat_version in compat_versions:
    nested_generated_imports_by_module[compat_module].remove(
        f'{output_package}.compat.v{compat_version}'
    )

  _gen_init_files(
      output_dir,
      output_package,
      api_version,
      nested_compat_symbols_by_module,
      nested_generated_imports_by_module,
      docs_by_module,
      root_template_path,
      file_prefixes_to_strip,
      use_lazy_loading,
      module_prefix,
      f'{compat_module}.v{api_version}',
  )

  for compat_version in compat_versions:
    nested_generated_imports_by_module[compat_module].add(
        f'{output_package}.compat.v{compat_version}'
    )
