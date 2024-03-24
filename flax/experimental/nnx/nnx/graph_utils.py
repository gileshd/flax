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

from __future__ import annotations

import dataclasses
import enum
import typing as tp

import jax

from flax.experimental.nnx.nnx import filterlib, reprlib, tracers
from flax.experimental.nnx.nnx.proxy_caller import (
  ApplyCaller,
  CallableProxy,
  DelayedAccessor,
)
from flax.experimental.nnx.nnx.state import State
from flax.experimental.nnx.nnx.variables import EMPTY, Empty, Variable
from flax.typing import Path, PathParts

A = tp.TypeVar('A')
B = tp.TypeVar('B')
C = tp.TypeVar('C')
HA = tp.TypeVar('HA', bound=tp.Hashable)
HB = tp.TypeVar('HB', bound=tp.Hashable)

Index = int
Names = tp.Sequence[int]
Node = tp.TypeVar('Node')
Leaf = tp.TypeVar('Leaf')
AuxData = tp.TypeVar('AuxData')

NODE_TYPES: dict[type, 'NodeImpl[tp.Any, tp.Any, tp.Any]'] = {}

class _HashById(tp.Hashable, tp.Generic[A]):
  """A wrapper around a value that uses its id for hashing and equality.
  This is used by RefMap to explicitly use object id as the hash for the keys.
  """

  __slots__ = ('_value',)

  def __init__(self, value: A):
    self._value = value

  @property
  def value(self) -> A:
    return self._value

  def __hash__(self) -> int:
    return id(self._value)

  def __eq__(self, other: tp.Any) -> bool:
    return isinstance(other, _HashById) and self._value is other._value


class RefMap(tp.MutableMapping[A, B], reprlib.MappingReprMixin[A, B]):
  """A mapping that uses object id as the hash for the keys."""

  def __init__(self):
    self._mapping: dict[_HashById[A], B] = {}

  def __getitem__(self, key: A) -> B:
    return self._mapping[_HashById(key)]

  def __setitem__(self, key: A, value: B):
    self._mapping[_HashById(key)] = value

  def __delitem__(self, key: A):
    del self._mapping[_HashById(key)]

  def __iter__(self) -> tp.Iterator[A]:
    return (x.value for x in self._mapping)

  def __len__(self) -> int:
    return len(self._mapping)

  def __str__(self) -> str:
    return repr(self)


@dataclasses.dataclass(frozen=True)
class NodeImplBase(tp.Generic[Node, Leaf, AuxData]):
  type: type
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[str, Leaf]], AuxData]]

  def node_dict(self, node: Node) -> dict[str, Leaf]:
    nodes, _ = self.flatten(node)
    return dict(nodes)


@dataclasses.dataclass(frozen=True)
class GraphNodeImpl(NodeImplBase[Node, Leaf, AuxData]):
  set_key: tp.Callable[[Node, str, Leaf], None]
  pop_key: tp.Callable[[Node, str], Leaf]
  create_empty: tp.Callable[[AuxData], Node]
  clear: tp.Callable[[Node, AuxData], None]
  update_static: tp.Callable[[Node, AuxData], None]

  def init(self, node: Node, items: tuple[tuple[str, Leaf], ...]):
    for key, value in items:
      self.set_key(node, key, value)


@dataclasses.dataclass(frozen=True)
class PytreeNodeImpl(NodeImplBase[Node, Leaf, AuxData]):
  unflatten: tp.Callable[[tuple[tuple[str, Leaf], ...], AuxData], Node]


NodeImpl = tp.Union[
  GraphNodeImpl[Node, Leaf, AuxData], PytreeNodeImpl[Node, Leaf, AuxData]
]



def register_graph_node_type(
  type: type,
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[str, Leaf]], AuxData]],
  set_key: tp.Callable[[Node, str, Leaf], None],
  pop_key: tp.Callable[[Node, str], Leaf],
  create_empty: tp.Callable[[AuxData], Node],
  clear: tp.Callable[[Node, AuxData], None],
  update_static: tp.Callable[[Node, AuxData], None],
):
  NODE_TYPES[type] = GraphNodeImpl(
    type=type,
    flatten=flatten,
    set_key=set_key,
    pop_key=pop_key,
    create_empty=create_empty,
    clear=clear,
    update_static=update_static,
  )


def is_node(x: tp.Any) -> bool:
  if isinstance(x, Variable):
    return False
  elif type(x) in NODE_TYPES:
    return True
  return is_pytree_node(x)

def is_graph_node(x: tp.Any) -> bool:
  return type(x) in NODE_TYPES


def is_node_type(x: type[tp.Any]) -> bool:
  return x in NODE_TYPES or x is PytreeType


def get_node_impl(x: Node) -> NodeImpl[Node, tp.Any, tp.Any]:
  if isinstance(x, Variable):
    raise ValueError(f'Variable is not a node: {x}')

  node_type = type(x)

  if node_type not in NODE_TYPES:
    if is_pytree_node(x):
      return PYTREE_NODE_IMPL
    else:
      raise ValueError(f'Unknown node type: {x}')

  return NODE_TYPES[node_type]


def get_node_impl_for_type(x: type[Node]) -> NodeImpl[Node, tp.Any, tp.Any]:
  if x is PytreeType:
    return PYTREE_NODE_IMPL
  return NODE_TYPES[x]


class _HashableMapping(tp.Mapping[HA, HB], tp.Hashable):
  def __init__(self, mapping: tp.Mapping[HA, HB] | tp.Iterable[tuple[HA, HB]]):
    self._mapping = dict(mapping)

  def __contains__(self, key: object) -> bool:
    return key in self._mapping

  def __getitem__(self, key: HA) -> HB:
    return self._mapping[key]

  def __iter__(self) -> tp.Iterator[HA]:
    return iter(self._mapping)

  def __len__(self) -> int:
    return len(self._mapping)

  def __hash__(self) -> int:
    return hash(tuple(sorted(self._mapping.items())))

  def __eq__(self, other: tp.Any) -> bool:
    return (
      isinstance(other, _HashableMapping) and self._mapping == other._mapping
    )

  def __repr__(self) -> str:
    return repr(self._mapping)


@dataclasses.dataclass(repr=False)
class _MappingRepr(reprlib.Representable):
  mapping: tp.Mapping[str, tp.Any]

  def __nnx_repr__(self):
    yield reprlib.Object(type='', value_sep=': ', start='{', end='}')

    for key, value in self.mapping.items():
      yield reprlib.Attr(repr(key), value)


class VariableDef(reprlib.Representable):
  __slots__ = (
    '_type',
    '_index',
    '_metadata',
  )

  def __init__(
    self,
    type: tp.Type[Variable[tp.Any]],
    index: int,
    metadata: dict[str, tp.Any],
  ):
    self._type = type
    self._index = index
    self._metadata = metadata

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))

    yield reprlib.Attr('type', self._type.__name__)
    yield reprlib.Attr('index', self._index)
    yield reprlib.Attr('metadata', _MappingRepr(self._metadata))

  @property
  def type(self):
    return self._type

  @property
  def index(self):
    return self._index

  @property
  def metadata(self):
    return self._metadata

  @classmethod
  def from_variable(cls, variable: Variable[tp.Any], index: int) -> VariableDef:
    metadata = vars(variable).copy()
    del metadata['raw_value']
    del metadata['_trace_state']
    return cls(type(variable), index, metadata)

  def to_variable(self, value: Node) -> Variable[Node]:
    # we use object.__new__ to avoid calling __init__ and bypass the
    # __init__ logic which should not be called twice
    variables = object.__new__(self._type)
    vars(variables).update(
      self._metadata, raw_value=value, _trace_state=tracers.TraceState()
    )
    return variables

  def __hash__(self):
    return hash((self._type, self._index, tuple(self._metadata.items())))

  def __eq__(self, other):
    if not isinstance(other, VariableDef):
      return False
    return (
      self._type == other._type
      and self._index == other._index
      and self._metadata == other._metadata
    )


class GraphDef(tp.Generic[Node], reprlib.Representable):
  __slots__ = (
    '_type',
    '_index',
    '_attributes',
    '_subgraphs',
    '_variables',
    '_metadata',
  )

  def __init__(
    self,
    type: tp.Type[Node],
    index: int,
    attributes: tuple[str, ...],
    subgraphs: tp.Iterable[tuple[str, tp.Union['GraphDef[tp.Any]', int]]],
    variables: tp.Iterable[tuple[str, VariableDef | int]],
    metadata: tp.Any,
  ):
    self._type: type[Node] = type
    self._index = index
    self._attributes = attributes
    self._subgraphs = _HashableMapping(subgraphs)
    self._variables = _HashableMapping(variables)
    self._metadata = metadata

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))

    yield reprlib.Attr('type', self._type.__name__)
    yield reprlib.Attr('index', self._index)
    yield reprlib.Attr('attributes', self._attributes)
    yield reprlib.Attr('subgraphs', _MappingRepr(self._subgraphs))
    yield reprlib.Attr('variables', _MappingRepr(self._variables))
    yield reprlib.Attr('metadata', self._metadata)

  def __hash__(self) -> int:
    return hash((self._type, self._subgraphs))

  def __eq__(self, other: tp.Any) -> bool:
    if not isinstance(other, GraphDef):
      return False
    return self._type == other._type and self._subgraphs == other._subgraphs

  @property
  def type(self) -> tp.Type[Node]:
    return self._type

  @property
  def index(self) -> int:
    return self._index

  @property
  def attributes(self) -> tuple[str, ...]:
    return self._attributes

  @property
  def subgraphs(self):
    return self._subgraphs

  @property
  def variables(self):
    return self._variables

  @property
  def metadata(self) -> tp.Any:
    return self._metadata

  def merge(self, state: State, /, *states: State) -> Node:
    if states:
      state = State.merge(state, *states)
    return graph_unflatten(self, state)[0]

  def apply(
    self, state: State, *states: State
  ) -> ApplyCaller[tuple[State, 'GraphDef[Node]']]:
    accessor = DelayedAccessor()

    def _apply(
      accessor: DelayedAccessor, *args, **kwargs
    ) -> tuple[tp.Any, tuple[State, GraphDef[Node]]]:
      module = self.merge(state, *states)
      fn = accessor(module)
      out = fn(*args, **kwargs)
      return out, graph_flatten(module)[:2]

    return CallableProxy(_apply, accessor)  # type: ignore

  def make_empty(self) -> Node:
    return self.merge(State({}))


def _gradphdef_flatten(graphdef: GraphDef[tp.Any]):
  return (), (
    graphdef._type,
    graphdef._index,
    graphdef._attributes,
    graphdef._subgraphs,
    graphdef._variables,
    graphdef._metadata,
  )


def _graphdef_unflatten(
  metadata: tuple[
    tp.Type[Node],
    int,
    tuple[str, ...],
    tuple[tuple[str, GraphDef[Node] | int], ...],
    tuple[tuple[str, tp.Any], ...],
    tuple[tuple[str, Variable[Empty] | int], ...],
    tp.Any,
  ],
  _,
) -> GraphDef[Node]:
  return GraphDef(*metadata)


jax.tree_util.register_pytree_node(
  GraphDef, _gradphdef_flatten, _graphdef_unflatten
)


def graph_flatten(
  x: Node,
) -> tuple[State, GraphDef[Node], tp.Mapping[tp.Any, Index]]:
  ref_to_index = RefMap[tp.Any, Index]()
  flat_state: dict[Path, Variable[tp.Any]] = {}
  graphdef = _graph_flatten((), ref_to_index, flat_state, x)
  assert not isinstance(graphdef, int)
  return State.from_flat_path(flat_state), graphdef, ref_to_index


def _graph_flatten(
  path: PathParts,
  ref_to_index: RefMap[tp.Any, Index],
  flat_state: dict[Path, Variable[tp.Any]],
  node: Node,
) -> GraphDef[Node] | int:
  if not is_node(node):
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')

  if node in ref_to_index:
    return ref_to_index[node]

  node_impl = get_node_impl(node)

  # only cache graph nodes
  if isinstance(node_impl, GraphNodeImpl):
    index = len(ref_to_index)
    ref_to_index[node] = index
  else:
    index = -1

  subgraphs: list[tuple[str, tp.Union[GraphDef[Node], int]]] = []
  variables: list[tuple[str, VariableDef | int]] = []

  values, metadata = node_impl.flatten(node)
  for key, value in values:
    if not isinstance(key, str):
      raise TypeError(
        f'Node (of type {type(node).__name__}) has a key of non-string '
        f'type {type(key).__name__}.'
      )
    if is_node(value):
      graphdef = _graph_flatten((*path, key), ref_to_index, flat_state, value)
      subgraphs.append((key, graphdef))
    elif isinstance(value, Variable):
      str_path = '/'.join((*path, key))
      if value in ref_to_index:
        variables.append((key, ref_to_index[value]))
      else:
        flat_state[str_path] = value.copy()
        variable_index = ref_to_index[value] = len(ref_to_index)
        variables.append(
          (key, VariableDef.from_variable(value, variable_index))
        )
    else:
      str_path = '/'.join((*path, key))
      flat_state[str_path] = value

  graphdef = GraphDef(
    type=node_impl.type,
    index=index,
    attributes=tuple(key for key, _ in values),
    subgraphs=subgraphs,
    variables=variables,
    metadata=metadata,
  )
  return graphdef


def graph_unflatten(
  graphdef: GraphDef[Node],
  state: State,
  /,
  *,
  ref_cache: dict[Index, tp.Any] | None = None,
) -> tuple[Node, dict[Index, tp.Any]]:
  """Unflattens a graphdef into a node with the given state.

  Args:
    graphdef: A GraphDef instance.
    state: A State instance.
    ref_cache: A mapping from indexes to existing nodes that can be reused.
      When an reference is reused, ``GraphNodeImpl.clear`` is called to leave the
      object in an empty state and then filled by the unflatten process, as a result
      existing graph nodes are mutated to have the new content/topology
      specified by the graphdef.
  """
  index_to_ref: dict[Index, tp.Any] = {}
  node = _graph_unflatten(graphdef, state.raw_mapping, index_to_ref, ref_cache)
  return node, index_to_ref


def _graph_unflatten(
  graphdef: tp.Union[GraphDef[Node], int],
  state: dict[str, Variable[tp.Any] | dict[str, tp.Any]],
  index_to_ref: dict[Index, tp.Any],
  ref_cache: dict[Index, tp.Any] | None,
) -> Node:
  """Recursive helper for graph_unflatten.

  Args:
    graphdef: A GraphDef instance or an index to a node in the cache.
    state: A mapping from attribute names to variables or subgraphs.
    index_to_ref: A mapping from indexes to nodes that have been traversed.
      If a node is already in the cache, it won't be traversed again.
    ref_cache: A mapping from indexes to existing nodes that can be reused.
      When an reference is reused, ``GraphNodeImpl.clear`` is called to leave the
      object in an empty state and then filled by the unflatten process, as a result
      existing graph nodes are mutated to have the new content/topology
      specified by the graphdef.
  """
  if isinstance(graphdef, int):
    return index_to_ref[graphdef]

  if not is_node_type(graphdef.type):
    raise RuntimeError(f'Unsupported type: {graphdef.type}, this is a bug.')

  if graphdef.index in index_to_ref:
    raise RuntimeError(f'GraphDef index {graphdef.index} already used.')

  # TODO(cgarciae): why copy here?
  state = state.copy()
  node_impl = get_node_impl_for_type(graphdef.type)

  def _get_children():
    new_state: dict[str, tp.Any] = {}

    for key in graphdef.attributes:
      if key not in state:
        # if key is not present create an empty types
        if key in graphdef.subgraphs:
          # if the key is a subgraph we create an empty node
          subgraphdef = graphdef.subgraphs[key]
          if isinstance(subgraphdef, int):
            # subgraph exists, take it from the cache
            new_state[key] = index_to_ref[subgraphdef]
          else:
            # create an empty node and add it to the cache
            substate = {}
            node = new_state[key] = _graph_unflatten(
              subgraphdef, substate, index_to_ref, ref_cache
            )
        elif key in graphdef.variables:
          variable_def = graphdef.variables[key]
          if isinstance(variable_def, int):
            # variable exists, take it from the cache
            new_state[key] = index_to_ref[variable_def]
          else:
            # create an empty variable and add it to the cache
            if ref_cache is not None and variable_def.index in ref_cache:
              node = ref_cache[variable_def.index]
              if type(node) != variable_def.type:
                raise ValueError(
                  f'Expected a node of type {variable_def.type.__name__} for '
                  f'index {variable_def.index}, but got a node of type '
                  f'{type(node).__name__}.'
                )
              assert isinstance(node, Variable)
              node.copy_from_def(variable_def, EMPTY)
            else:
              node = variable_def.to_variable(EMPTY)
            new_state[key] = node
            index_to_ref[variable_def.index] = node
        else:
          raise RuntimeError(f'Unknown field: {key!r}')
      else:
        value = state[key]
        if key in graphdef.subgraphs:
          if isinstance(value, Variable):
            raise ValueError(
              f'Expected a subgraph for {key!r}, but got a Variable.'
            )
          subgraphdef = graphdef.subgraphs[key]

          if isinstance(subgraphdef, int):
            node = index_to_ref[subgraphdef]
          else:
            node = new_state[key] = _graph_unflatten(
              subgraphdef, value, index_to_ref, ref_cache
            )

        elif key in graphdef.variables:
          variable_def = graphdef.variables[key]
          if isinstance(variable_def, int):
            new_state[key] = index_to_ref[variable_def]
          else:
            if type(value) != variable_def.type:
              raise ValueError(
                f'Expected a Variable of type {variable_def.type} '
                f'for {key!r}, but got a Variable of type {type(value)}.'
              )
            assert isinstance(value, Variable)
            if ref_cache is not None and variable_def.index in ref_cache:
              variable = ref_cache[variable_def.index]
              if type(variable) != variable_def.type:
                raise ValueError(
                  f'Expected a Variable of type {variable_def.type} for '
                  f'{key!r}, but got a Variable of type {type(variable)}.'
                )
              variable.copy_from(value)
            else:
              assert isinstance(value, Variable)
              variable = value.copy()
            new_state[key] = variable
            index_to_ref[variable_def.index] = variable
        else:
          new_state[key] = value

    for new_key in set(state) - set(graphdef.attributes):
      new_state[new_key] = state[new_key]

    return new_state

  if isinstance(node_impl, GraphNodeImpl):
    # we create an empty node first and add it to the index
    # this avoids infinite recursion when there is a reference cycle
    if ref_cache is not None and graphdef.index in ref_cache:
      node = ref_cache[graphdef.index]
      if type(node) != graphdef.type:
        raise ValueError(
          f'Expected a node of type {graphdef.type} for index '
          f'{graphdef.index}, but got a node of type {type(node)}.'
        )
      node_impl.clear(node, graphdef.metadata)
    else:
      node = node_impl.create_empty(graphdef.metadata)
    index_to_ref[graphdef.index] = node
    children = _get_children()
    node_impl.init(node, tuple(children.items()))
  else:
    # if the node type does not support the creation of an empty object it means
    # that it cannot reference itself, so we can create its children first
    children = _get_children()
    node = node_impl.unflatten(tuple(children.items()), graphdef.metadata)

  return node


def graph_pop(
  node: tp.Any,
  filters: tuple[filterlib.Filter, ...],
) -> tuple[State, ...]:
  id_to_index: dict[int, Index] = {}
  path_parts: PathParts = ()
  predicates = tuple(filterlib.to_predicate(filter) for filter in filters)
  states = tuple({} for _ in predicates)
  _graph_pop(node, id_to_index, path_parts, states, predicates)
  return tuple(State(x) for x in states)


def _graph_pop(
  node: tp.Any,
  id_to_index: dict[int, Index],
  path_parts: PathParts,
  states: tuple[dict[Path, tp.Any], ...],
  predicates: tuple[filterlib.Predicate, ...],
) -> None:
  if not is_node(node):
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')

  if id(node) in id_to_index:
    return

  id_to_index[id(node)] = len(id_to_index)
  node_impl = get_node_impl(node)
  node_dict = node_impl.node_dict(node)

  for name, value in node_dict.items():
    if is_node(value):
      _graph_pop(value, id_to_index, (*path_parts, name), states, predicates)
      continue
    elif not isinstance(value, Variable):
      continue
    elif id(value) in id_to_index:
      continue

    path = '/'.join((*path_parts, name))
    node_impl = get_node_impl(node)
    for state, predicate in zip(states, predicates):
      if predicate(path, value):
        if isinstance(node_impl, PytreeNodeImpl):
          raise ValueError(
            f'Cannot pop key {name!r} from node of type {type(node).__name__}'
          )
        state[path] = value.copy()
        id_to_index[id(value)] = len(id_to_index)
        node_impl.pop_key(node, name)
        break
    else:
      # NOTE: should we raise an error here?
      pass


def graph_update_dynamic(
  node: tp.Any,
  updates: State | tp.Sequence[State],
) -> None:
  if not is_node(node):
    raise ValueError(f'Unsupported type: {type(node)}')

  if isinstance(updates, State):
    new_states = (updates,)
  else:
    new_states = updates

  for state in new_states:
    _graph_update_dynamic(node, state.raw_mapping)


def _graph_update_dynamic(
  node: tp.Any, state: dict[str, Variable[tp.Any] | dict[str, tp.Any]]
):
  if not is_node(node):
    raise RuntimeError(f'Unsupported type: {type(node)}')

  node_impl = get_node_impl(node)
  node_dict = node_impl.node_dict(node)
  for key, value in state.items():
    # case 1: new state is being added
    if key not in node_dict:
      if isinstance(node_impl, PytreeNodeImpl):
        raise ValueError(
          f'Cannot set key {key!r} on immutable node of '
          f'type {type(node).__name__}'
        )
      if isinstance(value, Variable):
        value = value.copy()
      node_impl.set_key(node, key, value)
      continue

    # check values are of the same type
    current_value = node_dict[key]

    # case 2: subgraph is being updated
    if is_node(current_value):
      if isinstance(value, Variable):
        raise ValueError(
          f'Expected a subgraph for {key!r}, but got a Variable: {value!r}'
        )
      _graph_update_dynamic(current_value, value)
    elif isinstance(value, Variable):
      # case 3: Variable is being updated
      if not isinstance(current_value, Variable):
        raise ValueError(
          f'Trying to update a non-Variable attribute {key!r} with a Variable: '
          f'{value!r}'
        )
      current_value.copy_from(value)
    else:
      # case 4: non-Variable key is being updated
      if isinstance(node_impl, PytreeNodeImpl):
        raise ValueError(
          f'Cannot update key {key!r} on immutable node of '
          f'type {type(node).__name__}'
        )
      node_impl.set_key(node, key, value)


class _StaticModuleStatus(enum.Enum):
  NEW = enum.auto()
  UPDATED = enum.auto()


def graph_update_static(node: Node, updates: Node) -> None:
  cache: dict[int, _StaticModuleStatus] = {}
  _graph_update_static(node, updates, cache, _StaticModuleStatus.UPDATED, ())


def _graph_update_static(
  node: Node,
  updates: Node,
  cache: dict[int, _StaticModuleStatus],
  status: _StaticModuleStatus,
  path: PathParts,
) -> None:
  if type(node) != type(updates):
    raise ValueError(
      f'Trying to update a node with a different type: '
      f'expected {type(node).__name__!r}, '
      f'but got {type(updates).__name__!r}'
    )
  if not is_node(node):
    raise ValueError(f'Unsupported node type: {type(node)}')

  if id(updates) in cache:
    if cache[id(updates)] != status:
      str_path = '/'.join(path)
      if status is _StaticModuleStatus.NEW:
        raise ValueError(
          f'Trying to add a new node at path {str_path!r} but a'
          ' node with the same reference has been updated'
        )
      else:
        raise ValueError(
          f'Trying to update a node at path {str_path!r} but a new'
          ' node with the same reference has been added'
        )
    return

  cache[id(updates)] = status

  node_impl = get_node_impl(node)
  node_dict = node_impl.node_dict(node)
  updates_leaves, updates_static = node_impl.flatten(updates)
  updates_dict = dict(updates_leaves)
  if isinstance(node_impl, GraphNodeImpl):
    node_impl.update_static(node, updates_static)

  for name, value_updates in updates_dict.items():
    if is_node(value_updates):
      # case 1: updating an existing subgraph
      if name in node_dict:
        _graph_update_static(
          node_dict[name],
          value_updates,
          cache,
          _StaticModuleStatus.UPDATED,
          (*path, name),
        )
      else:
        # case 2: adding a new subgraph
        if isinstance(node_impl, PytreeNodeImpl):
          raise ValueError(
            f'Cannot set key {name!r} on immutable node of '
            f'type {type(node).__name__}'
          )

        # check if the subgraph is already in the cache
        if id(value_updates) in cache:
          # if its in the cache, check its status is not NEW
          if cache[id(value_updates)] is not _StaticModuleStatus.NEW:
            raise ValueError(
              f'Trying to add a new node at path {name!r} but a '
              'node with the same reference has been updated'
            )
        else:
          cache[id(value_updates)] = _StaticModuleStatus.NEW

        node_impl.set_key(node, name, value_updates)


def clone(node: Node) -> Node:
  state, static = graph_flatten(node)[:2]
  return static.merge(state)


def iter_nodes(node: tp.Any) -> tp.Iterator[tuple[Path, tp.Any]]:
  visited: set[int] = set()
  path_parts: PathParts = ()
  yield from _iter_nodes(node, visited, path_parts)


def _iter_nodes(
  node: tp.Any, visited: set[int], path_parts: PathParts
) -> tp.Iterator[tuple[Path, tp.Any]]:
  if not is_node(node):
    return
  if id(node) in visited:
    return
  visited.add(id(node))
  path = '/'.join(path_parts)
  yield path, node
  node_impl = get_node_impl(node)
  node_dict = node_impl.node_dict(node)
  for key, value in node_dict.items():
    yield from _iter_nodes(value, visited, (*path_parts, key))


def compose_mapping(
  map_ab: tp.Mapping[A, B], map_bc: tp.Mapping[B, C], /
) -> dict[A, C]:
  return {a: map_bc[b] for a, b in map_ab.items() if b in map_bc}


def compose_mapping_reversed(
  map_ab: tp.Mapping[A, B], map_bc: tp.Mapping[B, C], /
) -> dict[C, A]:
  return {map_bc[b]: a for a, b in map_ab.items() if b in map_bc}


@dataclasses.dataclass(frozen=True)
class Static(tp.Generic[A]):
  """An empty pytree node that treats its inner value as static.
  ``value`` must define ``__eq__`` and ``__hash__``.
  """

  value: A


jax.tree_util.register_static(Static)


# Pytree
class PytreeType:
  ...


def is_pytree_node(x: tp.Any) -> bool:
  return not jax.tree_util.all_leaves([x])


def _key_path_to_str(key: tp.Any) -> str:
  if isinstance(key, jax.tree_util.SequenceKey):
    return str(key.idx)
  elif isinstance(
    key, (jax.tree_util.DictKey, jax.tree_util.FlattenedIndexKey)
  ):
    return str(key.key)
  elif isinstance(key, jax.tree_util.GetAttrKey):
    return key.name
  else:
    return str(key)


def _flatten_pytree(pytree: tp.Any):
  leaves, treedef = jax.tree_util.tree_flatten_with_path(
    pytree, is_leaf=lambda x: x is not pytree
  )
  nodes = tuple((_key_path_to_str(path[0]), value) for path, value in leaves)

  return nodes, treedef


def _unflatten_pytree(
  nodes: tuple[tuple[str, tp.Any], ...], treedef: jax.tree_util.PyTreeDef
):
  pytree = treedef.unflatten(value for _, value in nodes)
  return pytree

PYTREE_NODE_IMPL = PytreeNodeImpl(
  type=PytreeType,
  flatten=_flatten_pytree,
  unflatten=_unflatten_pytree,
)
