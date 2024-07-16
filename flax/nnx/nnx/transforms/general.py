# Copyright 2024 The Flax Authors.
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

# Copyright 2023 The Flax Authors.
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

import functools
import typing as tp

from flax import struct
from flax.nnx.nnx import (
  extract,
  graph,
)
from flax.nnx.nnx.module import GraphDef
from flax.nnx.nnx.state import State

A = tp.TypeVar('A')
F = tp.TypeVar('F', bound=tp.Callable[..., tp.Any])

class Missing:
  pass


MISSING = Missing()

# -------------------------------
# (split|merge)_inputs
# -------------------------------


class ArgState(extract.ExtractionIndex, extract.ExtractableStates):
  _graphdef: GraphDef[tp.Any] = struct.field(pytree_node=False)
  state: State = struct.field(pytree_node=True)

  @property
  def graphdef(self) -> GraphDef[tp.Any]:
    return self._graphdef

  @property
  def states(self) -> tp.Iterable[State]:
    yield self.state

@tp.overload
def split_inputs(
  *,
  ctx_tag: str = 'split_merge_inputs',
) -> tp.Callable[[F], F]: ...
@tp.overload
def split_inputs(
  f: F,
  *,
  ctx_tag: str = 'split_merge_inputs',
) -> F: ...
def split_inputs(
  f: F | Missing = MISSING,
  *,
  ctx_tag: str = 'split_merge_inputs',
) -> F | tp.Callable[[F], F]:
  """Takes in a function that contains graph nodes in the inputs and outputs, and
  returns a function that replaces them with :class:`ArgState` instances which are
  jax-compatible data structures (pytrees). Must be used in conjunction with
  :func:`merge_inputs`.

  Args:
    f: The function to be transformed.
    ctx_tag: The context tag to be used for the transformation. Defaults to
      'split_merge_inputs'.

  Returns:
    The transformed function.

  ``split_inputs`` and ``merge_inputs`` can be used to lift functions that operate
  on jax datastructures (pytrees) to functions that operate on graph nodes. ``split_inputs``
  will take graph nodes in the inputs and outputs and replace them with :class:`ArgState`
  instances, usually before calling into the transformed function, while ``merge_inputs``
  will convert :class:`ArgState` instances back to graph nodes, usually inside the transformed
  function. For common transforms like ``jax.jit`` and ``jax.vmap`` NNX will provide a version
  that works with graph nodes, but for other transforms you can use ``split_inputs`` and
  ``merge_inputs`` to manually lift the function.

  The following example demonstrates how to use ``split_inputs`` and ``merge_inputs`` to
  lift ``jax.jit`` to work over a silly function has a stateful operation that zeros out
  the kernel of a linear layer::

    >>> from flax import nnx
    >>> import jax.numpy as jnp
    >>> import jax
    ...
    >>> @nnx.split_inputs
    ... @jax.jit
    ... @nnx.merge_inputs
    ... def forward_and_zero(model: nnx.Linear, x: jax.Array):
    ...   y = model(x)
    ...   model.kernel *= 0  # zero out the kernel
    ...   return y
    ...
    >>> model = nnx.Linear(2, 2, rngs=nnx.Rngs(0))
    >>> y = forward_and_zero(model, jnp.ones((1, 2)))
    >>> y.shape
    (1, 2)
    >>> assert jnp.allclose(model.kernel, 0)

  As shown above, not only does the lifted function work with graph nodes, but it also
  propagates the side effects of the original function. **Note**: in practice use ``nnx.jit``
  instead.

  Splitting and merging can also be applied to multiple functions in a pipeline. The following
  example show how to lift ``jax.lax.cond`` by using ``split_inputs`` over ``cond`` and
  ``merge_inputs`` over the branches::

    >>> model = nnx.Linear(2, 2, rngs=nnx.Rngs(0))
    >>> x = jnp.ones((1, 2))
    ...
    >>> true_fn = lambda m, x: m(x)
    >>> false_fn = lambda m, x: x + 1
    ...
    >>> y = nnx.split_inputs(jax.lax.cond)(
    ...   False,
    ...   nnx.merge_inputs(true_fn),
    ...   nnx.merge_inputs(false_fn), <== gets called
    ...   model,
    ...   x,
    ... )
    >>> assert jnp.allclose(y, 2)

    **Lifting functions with output semantics**

    ``merge_inputs`` internally returns a `(inputs, output)` tuple, where `inputs` is the
    tuple of the input arguments non-graph node leaves set to `None`, and `output` is the
    output of the function. This is done to propage all the state changes in the function
    to the graph nodes outside the function. If the transform function has output semantics
    like e.g. ``jax.vmap``'s ``out_axes`` argument, you must account for this in the by
    configuring the arguments accordingly::

    >>> from functools import partial
    ...
    >>> model = nnx.Linear(2, 2, rngs=nnx.Rngs(0))
    ...
    >>> in_axes = (None, 0)
    >>> out_axes = (in_axes, 0)  # <== internal output arrangement
    ...
    >>> @nnx.split_inputs
    ... @partial(jax.vmap, in_axes=in_axes, out_axes=out_axes)
    ... @nnx.merge_inputs
    ... def forward(model: nnx.Linear, x: jax.Array):
    ...   return model(x)
    ...
    >>> x = jnp.ones((10, 2))
    >>> y = forward(model, x)
    >>> y.shape
    (10, 2)

  **NOTE**: If the transform has a rigid output (e.g. ``jax.grad`` or ``jax.lax.scan``) then
  ``split_inputs`` and ``merge_inputs`` will not work. In this case, use the
  `Functional API <https://flax.readthedocs.io/en/latest/nnx/nnx_basics.html#the-functional-api>`__.
  """
  if isinstance(f, Missing):
    return functools.partial(split_inputs, ctx_tag=ctx_tag)

  @graph.update_context(ctx_tag)
  @functools.wraps(f)
  def split_inputs_wrapper(*args):
    ctx = graph.current_update_context(ctx_tag)
    args, input_graph_nodes = extract.extract_graph_nodes(args)
    graphdef, states = ctx.split(input_graph_nodes)
    args = extract.replace_indexes(
      args,
      lambda x: ArgState(
        x.index,
        graphdef,
        states[x.index],  # type: ignore
      ),
    )
    args_out, out = f(*args)
    arg_states_out = extract.extract_indexes((args_out, out), types=ArgState)

    if arg_states_out:
      graphdef_out, states_out = extract.merge_extractable_states(
        arg_states_out
      )
      output_nodes = ctx.merge(graphdef_out, states_out)
      out = extract.insert_graph_nodes(out, output_nodes)

    return out

  return split_inputs_wrapper  # type: ignore

@tp.overload
def merge_inputs(
  *,
  ctx_tag: str = 'split_merge_inputs',
) -> tp.Callable[[F], F]: ...
@tp.overload
def merge_inputs(
  f: F,
  *,
  ctx_tag: str = 'split_merge_inputs',
) -> F: ...
def merge_inputs(
  f: F | Missing = MISSING,
  *,
  ctx_tag: str = 'split_merge_inputs',
) -> F | tp.Callable[[F], F]:
  """Takes in a functions with

  Args:
    f: The function to be transformed.
    ctx_tag: The context tag to be used for the transformation. Defaults to
      'split_merge_inputs'.

  Returns:
    The transformed function.
  """
  if isinstance(f, Missing):
    return functools.partial(merge_inputs, ctx_tag=ctx_tag)

  @functools.wraps(f)
  def merge_inputs_wrapper(*args):
    ctx = graph.current_update_context(ctx_tag)
    arg_states = extract.extract_indexes(args, types=ArgState)

    if arg_states:
      graphdef, states = extract.merge_extractable_states(arg_states)
      inputs_graph_nodes = ctx.merge(graphdef, states)
      args = extract.insert_graph_nodes(args, inputs_graph_nodes)

    out = f(*args)

    (args_out, out), output_graph_nodes = extract.extract_graph_nodes(
      (args, out)
    )

    graphdef_out, states_out = ctx.split(output_graph_nodes)

    def replace_index(x: extract.Extractable):
      return ArgState(
        x.index,
        graphdef_out,
        states_out[x.index],  # type: ignore
      )

    out = extract.replace_indexes(out, replace_index)
    args_out = extract.replace_indexes(args_out, replace_index, clear=True)

    return args_out, out

  return merge_inputs_wrapper  # type: ignore
