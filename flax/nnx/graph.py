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

from collections import deque
import contextlib
import dataclasses
import functools
import threading
import typing as tp
from weakref import WeakKeyDictionary

import jax
import numpy as np
import typing_extensions as tpe

from flax.nnx import filterlib, reprlib
from flax.nnx.proxy_caller import (
  ApplyCaller,
  CallableProxy,
  DelayedAccessor,
)
from flax.nnx.statelib import FlatState, State
from flax.nnx import variablelib
from flax.nnx.variablelib import Variable, VariableState
from flax.typing import Key, PathParts, is_key_like

A = tp.TypeVar('A')
B = tp.TypeVar('B')
C = tp.TypeVar('C')
F = tp.TypeVar('F', bound=tp.Callable)

HA = tp.TypeVar('HA', bound=tp.Hashable)
HB = tp.TypeVar('HB', bound=tp.Hashable)
KeyT = tp.TypeVar('KeyT', bound=Key)

Index = int
Names = tp.Sequence[int]
Node = tp.TypeVar('Node')
Leaf = tp.TypeVar('Leaf')
AuxData = tp.TypeVar('AuxData')

StateLeaf = VariableState[tp.Any]
NodeLeaf = Variable[tp.Any]
GraphState = State[Key, StateLeaf]
GraphFlatState = FlatState[StateLeaf]


def is_state_leaf(x: tp.Any) -> tpe.TypeGuard[StateLeaf]:
  return isinstance(x, VariableState)


def is_node_leaf(x: tp.Any) -> tpe.TypeGuard[NodeLeaf]:
  return isinstance(x, Variable)


# class RefMap(reprlib.MappingReprMixin, tp.Generic[A, B]):
#   """A mapping that uses object id as the hash for the keys."""

#   def __init__(self, mapping: dict[A, B] | tp.Iterable[tuple[A, B]] = (), /):
#     items = tp.cast(
#       tp.Iterable[tuple[A, B]],
#       mapping.items() if isinstance(mapping, dict) else mapping,
#     )
#     self._mapping = {id(key): (key, value) for key, value in items}

#   def copy(self) -> RefMap[A, B]:
#     return RefMap(self.items())

#   def update(self, mapping: tp.Mapping[A, B]):
#     self._mapping.update(
#       (id(key), (key, value)) for key, value in mapping.items()
#     )

#   def items(self) -> tp.Iterator[tuple[A, B]]:
#     return iter((key, value) for idx, (key, value) in self._mapping.items())

#   def __getitem__(self, key: A) -> B:
#     return self._mapping[id(key)][1]

#   def __contains__(self, key: object) -> bool:
#     return id(key) in self._mapping

#   def __setitem__(self, key: A, value: B):
#     self._mapping[id(key)] = (key, value)

#   def __delitem__(self, key: A):
#     del self._mapping[id(key)]

#   def __iter__(self) -> tp.Iterator[A]:
#     return (key for key, _ in self._mapping.values())

#   def __len__(self) -> int:
#     return len(self._mapping)

#   def __str__(self) -> str:
#     return repr(self)

RefMap = dict

@dataclasses.dataclass(frozen=True, slots=True)
class NodeImplBase(tp.Generic[Node, Leaf, AuxData]):
  type: type[Node]
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[Key, Leaf]], AuxData]]

  def node_dict(self, node: Node) -> dict[Key, Leaf]:
    nodes, _ = self.flatten(node)
    return dict(nodes)


@dataclasses.dataclass(frozen=True, slots=True)
class GraphNodeImpl(NodeImplBase[Node, Leaf, AuxData]):
  set_key: tp.Callable[[Node, Key, Leaf], None]
  pop_key: tp.Callable[[Node, Key], Leaf]
  create_empty: tp.Callable[[AuxData], Node]
  clear: tp.Callable[[Node], None]
  init: tp.Callable[[Node, tp.Iterable[tuple[Key, Leaf]]], None]


@dataclasses.dataclass(frozen=True, slots=True)
class PytreeNodeImpl(NodeImplBase[Node, Leaf, AuxData]):
  unflatten: tp.Callable[[tp.Sequence[tuple[Key, Leaf]], AuxData], Node]


NodeImpl = tp.Union[
  GraphNodeImpl[Node, Leaf, AuxData], PytreeNodeImpl[Node, Leaf, AuxData]
]


GRAPH_REGISTRY: dict[type, NodeImpl[tp.Any, tp.Any, tp.Any]] = {}
PYTREE_REGISTRY: dict[type, PytreeNodeImpl[tp.Any, tp.Any, tp.Any]] = {}


def register_graph_node_type(
  type: type,
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[Key, Leaf]], AuxData]],
  set_key: tp.Callable[[Node, Key, Leaf], None],
  pop_key: tp.Callable[[Node, Key], Leaf],
  create_empty: tp.Callable[[AuxData], Node],
  clear: tp.Callable[[Node], None],
  init: tp.Callable[[Node, tp.Iterable[tuple[Key, Leaf]]], None],
):
  if type in GRAPH_REGISTRY:
    raise ValueError(f'Node type {type} is already registered.')

  GRAPH_REGISTRY[type] = GraphNodeImpl(
    type=type,
    flatten=flatten,
    set_key=set_key,
    pop_key=pop_key,
    create_empty=create_empty,
    clear=clear,
    init=init,
  )

def register_pytree_node_type(
  type: type,
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[Key, Leaf]], AuxData]],
  unflatten: tp.Callable[[tp.Sequence[tuple[Key, Leaf]], AuxData], Node],
):
  if type in PYTREE_REGISTRY:
    raise ValueError(f'Node type {type} is already registered.')

  PYTREE_REGISTRY[type] = PytreeNodeImpl(
    type=type, flatten=flatten, unflatten=unflatten
  )

def is_node(x: tp.Any) -> bool:
  if type(x) in GRAPH_REGISTRY:
    return True
  return is_pytree_node(x)


def is_graph_node(x: tp.Any) -> bool:
  return type(x) in GRAPH_REGISTRY


def is_node_type(x: type[tp.Any]) -> bool:
  return x in GRAPH_REGISTRY or x in PYTREE_REGISTRY or x is GenericPytree


def get_node_impl(x: Node) -> NodeImpl[Node, tp.Any, tp.Any] | None:
  if isinstance(x, Variable):
    return None

  node_type = type(x)

  if node_type in GRAPH_REGISTRY:
    return GRAPH_REGISTRY[node_type]
  elif node_type in PYTREE_REGISTRY:
    return PYTREE_REGISTRY[node_type]
  elif node_type in JAX_PYTREE_REGISTRY or issubclass(node_type, tuple):
    return PYTREE_NODE_IMPL  # type: ignore
  else:
    return None


def get_node_impl_for_type(
  x: type[Node],
) -> NodeImpl[Node, tp.Any, tp.Any] | None:
  if x is GenericPytree:
    return PYTREE_NODE_IMPL  # type: ignore
  elif x in PYTREE_REGISTRY:
    return PYTREE_REGISTRY[x]
  elif x in GRAPH_REGISTRY:
    return GRAPH_REGISTRY[x]
  else:
    return None


class HashableMapping(tp.Mapping[HA, HB], tp.Hashable):
  def __init__(self, mapping: tp.Mapping[HA, HB], copy: bool = True):
    self._mapping = dict(mapping) if copy else mapping

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
      isinstance(other, HashableMapping) and self._mapping == other._mapping
    )

  def __repr__(self) -> str:
    return repr(self._mapping)


@dataclasses.dataclass(frozen=True, repr=False)
class NodeRef(tp.Generic[Node], reprlib.Representable):
  type: type[Node]
  index: int

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    yield reprlib.Attr('type', self.type.__name__)
    yield reprlib.Attr('index', self.index)

  def __treescope_repr__(self, path, subtree_renderer):
    import treescope  # type: ignore[import-not-found,import-untyped]
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={'type': self.type, 'index': self.index},
      path=path,
      subtree_renderer=subtree_renderer,
    )


jax.tree_util.register_static(NodeRef)

@dataclasses.dataclass(frozen=True, repr=False)
class VariableDef(reprlib.Representable):
  type: type[Variable]
  index: int
  outer_index: int | None
  metadata: HashableMapping[str, tp.Any]

  def with_no_outer_index(self) -> VariableDef:
    return VariableDef(
      type=self.type, index=self.index, outer_index=None, metadata=self.metadata
    )

  def with_same_outer_index(self) -> VariableDef:
    return VariableDef(
      type=self.type,
      index=self.index,
      outer_index=self.index,
      metadata=self.metadata,
    )

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    yield reprlib.Attr('type', self.type.__name__)
    yield reprlib.Attr('index', self.index)
    yield reprlib.Attr('outer_index', self.outer_index)
    yield reprlib.Attr('metadata', reprlib.PrettyMapping(self.metadata))

  def __treescope_repr__(self, path, subtree_renderer):
    import treescope  # type: ignore[import-not-found,import-untyped]

    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={
        'type': self.type,
        'index': self.index,
        'metadata': self.metadata,
      },
      path=path,
      subtree_renderer=subtree_renderer,
    )


jax.tree_util.register_static(VariableDef)


@dataclasses.dataclass(frozen=True, repr=False, slots=True)
class NodeDef(tp.Generic[Node], reprlib.Representable):
  """A dataclass that denotes the tree structure of a
  :class:`Module`. A ``GraphDef`` can be generated by either
  calling :func:`split` or :func:`graphdef` on the :class:`Module`."""

  type: tp.Type[Node]
  index: int
  outer_index: int | None
  attributes: tuple[
    tuple[
      Key, NodeDef[tp.Any] | VariableDef | NodeRef[tp.Any] | Static[tp.Any]
    ],
    ...,
  ]
  metadata: tp.Any

  def with_no_outer_index(self) -> NodeDef[Node]:
    attributes = tuple(
      (
        key,
        value.with_no_outer_index()
        if isinstance(value, NodeDef | VariableDef)
        else value,
      )
      for key, value in self.attributes
    )
    return NodeDef(
      type=self.type,
      index=self.index,
      outer_index=None,
      attributes=attributes,
      metadata=self.metadata,
    )

  def with_same_outer_index(self) -> NodeDef[Node]:
    attributes = tuple(
      (
        key,
        value.with_same_outer_index()
        if isinstance(value, NodeDef | VariableDef)
        else value,
      )
      for key, value in self.attributes
    )
    return NodeDef(
      type=self.type,
      index=self.index,
      outer_index=self.index if self.index >= 0 else None,
      attributes=attributes,
      metadata=self.metadata,
    )

  def replace(self, **kwargs):
    return dataclasses.replace(self, **kwargs)

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))

    yield reprlib.Attr('type', self.type.__name__)
    yield reprlib.Attr('index', self.index)
    yield reprlib.Attr('outer_index', self.outer_index)
    yield reprlib.Attr('attributes', self.attributes)
    yield reprlib.Attr('metadata', self.metadata)

  def __treescope_repr__(self, path, subtree_renderer):
    import treescope  # type: ignore[import-not-found,import-untyped]
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={
        'type': self.type,
        'index': self.index,
        'attributes': self.attributes,
        'metadata': self.metadata,
      },
      path=path,
      subtree_renderer=subtree_renderer,
    )

  def apply(
    self, state: GraphState, *states: GraphState
  ) -> ApplyCaller[tuple[GraphDef[Node], GraphState]]:
    accessor = DelayedAccessor()

    def _apply(
      accessor: DelayedAccessor, *args, **kwargs
    ) -> tuple[tp.Any, tuple[GraphDef[Node], GraphState]]:
      module = merge(self, state, *states)
      fn = accessor(module)
      out = fn(*args, **kwargs)
      graphdef, flat_state = flatten(module)
      state_ = State.from_flat_path(flat_state)
      return out, (graphdef, state_)

    return CallableProxy(_apply, accessor)  # type: ignore


jax.tree_util.register_static(NodeDef)

GraphDef = tp.Union[NodeDef[Node], NodeRef[Node]]
PureState = tuple[GraphDef[Node], GraphState]


@tp.overload
def flatten(
  node: Node,
  /,
  *,
  ref_index: RefMap[tp.Any, Index] | None = None,
  ref_outer_index: RefMap[tp.Any, Index] | None = None,
) -> tuple[GraphDef[Node], FlatState[VariableState[tp.Any]]]: ...
@tp.overload
def flatten(
  node: Node,
  /,
  *,
  with_paths: tp.Literal[True],
  return_variables: tp.Literal[True],
  ref_index: RefMap[tp.Any, Index] | None = None,
  ref_outer_index: RefMap[tp.Any, Index] | None = None,
) -> tuple[
  GraphDef[Node],
  FlatState[Variable[tp.Any]],
]: ...
@tp.overload
def flatten(
  node: Node,
  /,
  *,
  with_paths: bool,
  ref_index: RefMap[tp.Any, Index] | None = None,
  ref_outer_index: RefMap[tp.Any, Index] | None = None,
) -> tuple[
  GraphDef[Node],
  FlatState[VariableState[tp.Any]] | list[tp.Any],
]: ...
def flatten(
  node: Node,
  /,
  *,
  with_paths: bool = True,
  return_variables: bool = False,
  ref_index: RefMap[tp.Any, Index] | None = None,
  ref_outer_index: RefMap[tp.Any, Index] | None = None,
) -> tuple[
  GraphDef[Node],
  FlatState[VariableState[tp.Any]] | FlatState[Variable[tp.Any]] | list[tp.Any],
]:
  """Flattens a graph node into a (graphdef, state) pair.

  Args:
    x: A graph node.
    ref_index: A mapping from nodes to indexes, defaults to None. If not provided, a new
      empty dictionary is created. This argument can be used to flatten a sequence of graph
      nodes that share references.
    with_paths: A boolean that indicates whether to return a FlatState object that includes
      the paths to VariableState objects, or just a list of the Variable's inner values.
  """
  if ref_index is None:
    ref_index = RefMap()

  leaves: list[StateLeaf | Variable[tp.Any]] = []
  path: list[Key] | None = [] if with_paths else None
  paths: list[PathParts] | None = [] if with_paths else None
  node_impl = get_node_impl(node)
  if node_impl is None:
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')
  graphdef = _graph_flatten(
    node,
    node_impl,
    path,
    ref_index,
    ref_outer_index,
    leaves,
    paths,
    return_variables,
  )

  if paths is not None:
    return graphdef, FlatState.from_sorted_keys_values(tuple(paths), leaves)
  else:
    return graphdef, leaves


def _graph_flatten(
  node: Node,
  node_impl: NodeImpl[Node, Leaf, AuxData],
  path: list[Key] | None,
  ref_index: RefMap[tp.Any, Index],
  ref_outer_index: RefMap[tp.Any, Index] | None,
  leaves: list[StateLeaf | Variable[tp.Any]],
  paths: list[PathParts] | None,
  return_variables: bool,
) -> NodeDef[Node] | NodeRef:
  is_pytree_node_ = isinstance(node_impl, PytreeNodeImpl)
  is_graph_node_ = isinstance(node_impl, GraphNodeImpl)

  if not is_pytree_node_ and node in ref_index:
    return NodeRef(type(node), ref_index[node])

  # only cache graph nodes
  if is_graph_node_:
    index = len(ref_index)
    ref_index[node] = index
  else:
    index = -1

  attributes: list[
    tuple[Key, Static[tp.Any] | NodeDef[tp.Any] | VariableDef | NodeRef[tp.Any]]
  ] = []

  values, metadata = node_impl.flatten(node)
  for key, value in values:
    value_node_impl = get_node_impl(value)
    if path is not None:
      path.append(key)
    if value_node_impl is not None:
      nodedef = _graph_flatten(
        value,
        value_node_impl,
        path,
        ref_index,
        ref_outer_index,
        leaves,
        paths,
        return_variables,
      )
      attributes.append((key, nodedef))
    elif isinstance(value, Variable):
      if value in ref_index:
        attributes.append((key, NodeRef(type(value), ref_index[value])))
      else:
        if return_variables:
          leaf = value
        elif path is None:
          leaf = value.raw_value
        else:
          leaf = value.to_state()
        leaves.append(leaf)
        if path is not None:
          assert paths is not None
          paths.append(tuple(path))
        variable_index = ref_index[value] = len(ref_index)
        variabledef = VariableDef(
          type=type(value),
          index=variable_index,
          outer_index=ref_outer_index.get(value, None)
          if ref_outer_index
          else None,
          metadata=HashableMapping(value._var_metadata),
        )
        attributes.append((key, variabledef))
    else:
      if isinstance(value, (jax.Array, np.ndarray)):
        if path is not None:
          path_str = '/'.join(map(str, path))
          raise ValueError(
            f'Arrays leaves are not supported, at {path_str!r}: {value}'
          )
        else:
          raise ValueError(f'Arrays leaves are not supported, found {value}')
      # static_fields.append((key, value))
      attributes.append((key, Static(value)))

    if path is not None:
      path.pop()

  nodedef = NodeDef(
    type=node_impl.type,
    index=index,
    outer_index=ref_outer_index[node]
    if is_graph_node_ and ref_outer_index and node in ref_outer_index
    else None,
    attributes=tuple(attributes),
    metadata=metadata,
  )
  return nodedef


def fingerprint(
  node,
  /,
  *,
  ref_index: RefMap[tp.Any, Index] | None = None,
  new_ref_index: RefMap[tp.Any, Index] | None = None,
) -> tuple[tp.Any, ...]:
  """ """
  if ref_index is None:
    ref_index = RefMap()

  if new_ref_index is None:
    new_ref_index = RefMap()
  next_index = len(ref_index) + len(new_ref_index)
  node_impl = get_node_impl(node)
  if node_impl is None:
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')
  fp, next_index = _graph_fingerprint(
    node, node_impl, ref_index, new_ref_index, next_index
  )
  return fp


def _graph_fingerprint(
  node,
  node_impl: NodeImpl[Node, Leaf, AuxData],
  ref_index: RefMap[tp.Any, Index],
  new_ref_index: RefMap[tp.Any, Index],
  next_index: int,
) -> tuple[tuple[tp.Any, ...], int]:
  is_pytree_node_ = type(node_impl) is PytreeNodeImpl
  is_graph_node_ = type(node_impl) is GraphNodeImpl

  if is_pytree_node_:
    pass
  elif node in ref_index:
    return (id(node), type(node), ref_index[node]), next_index
  elif node in new_ref_index:
    return (id(node), type(node), new_ref_index[node]), next_index

  # only cache graph nodes
  if is_graph_node_:
    index = new_ref_index[node] = next_index
    next_index += 1
  else:
    index = -1

  attributes: list[tuple[tp.Any, ...]] = []

  values, metadata = node_impl.flatten(node)
  for key, value in values:
    value_node_impl = get_node_impl(value)
    if value_node_impl is not None:
      node_fp, next_index = _graph_fingerprint(
        value, value_node_impl, ref_index, new_ref_index, next_index
      )
      attributes.append((key, node_fp))
    elif isinstance(value, Variable):
      if value in ref_index:
        attributes.append((key, id(value), type(value), ref_index[value]))
      elif value in new_ref_index:
        attributes.append((key, id(value), type(value), new_ref_index[value]))
      else:
        variable_index = new_ref_index[value] = next_index
        next_index += 1
        # the fingerprint must be sensitive to Variable identity
        attributes.append(
          (
            key,
            id(value),
            type(value),
            variable_index,
            tuple(value._var_metadata.items()),
          )
        )
    else:
      if isinstance(value, (jax.Array, np.ndarray)):
        raise ValueError(f'Arrays leaves are not supported: {value}')
      attributes.append((key, value))

  node_fp = (
    id(node) if is_graph_node_ else None,
    node_impl.type,
    index,
    tuple(attributes),
    metadata,
  )
  return node_fp, next_index


def _get_sorted_leaves(
  xs: tp.Mapping[tp.Any, tp.Any],
) -> deque[tp.Any]:
  if not isinstance(xs, tp.Mapping):  # type: ignore
    raise TypeError(f'expected Mapping; got {type(xs).__qualname__}')
  leaves = deque()

  def _flatten(xs):
    if not isinstance(xs, tp.Mapping):
      leaves.append(xs)
    else:
      for _, value in sorted(xs.items()):
        _flatten(value)

  _flatten(xs)
  return leaves


def unflatten(
  graphdef: GraphDef[Node],
  state: State[KeyT, tp.Any | dict[KeyT, tp.Any]]
  | FlatState[tp.Any]
  | list[tp.Any],
  /,
  *,
  index_ref: dict[Index, tp.Any] | None = None,
  outer_index_outer_ref: dict[Index, tp.Any] | None = None,
) -> Node:
  """Unflattens a graphdef into a node with the given state.

  Args:
    graphdef: A GraphDef instance.
    state: A State instance.
    index_ref: A mapping from indexes to nodes references found during the graph
      traversal, defaults to None. If not provided, a new empty dictionary is
      created. This argument can be used to unflatten a sequence of (graphdef, state)
      pairs that share the same index space.
    index_ref_cache: A mapping from indexes to existing nodes that can be reused.
      When an reference is reused, ``GraphNodeImpl.clear`` is called to leave the
      object in an empty state and then filled by the unflatten process, as a result
      existing graph nodes are mutated to have the new content/topology
      specified by the graphdef.
  """
  if isinstance(state, (State, dict)):
    leaves = _get_sorted_leaves(state)
  elif isinstance(state, FlatState):
    leaves = deque(state.get_values())
  elif isinstance(state, list):  # type: ignore
    leaves = deque(state)
  else:
    raise ValueError(f'Unsupported state type: {type(state)}')
  if index_ref is None:
    index_ref = {}

  if isinstance(graphdef, NodeRef):
    node = index_ref[graphdef.index]
  else:
    assert isinstance(graphdef, NodeDef)
    node_impl = get_node_impl_for_type(graphdef.type)
    if node_impl is None:
      raise RuntimeError(f'Unsupported type: {graphdef.type}, this is a bug.')
    node = _graph_unflatten(
      graphdef, node_impl, leaves, index_ref, outer_index_outer_ref
    )
  if leaves:
    raise ValueError(
      f'Incorrect number of leaves: got an extra {len(leaves)} leaves in the state'
    )

  return node


def _graph_unflatten(
  nodedef: NodeDef[Node] | NodeRef[Node],
  node_impl: NodeImpl[Node, Leaf, AuxData],
  leaves: deque[tp.Any],
  index_ref: dict[Index, tp.Any],
  outer_index_outer_ref: dict[Index, tp.Any] | None,
) -> Node:
  """Recursive helper for graph_unflatten.

  Args:
    nodedef: A GraphDef instance or an index to a node in the cache.
    state: A mapping from attribute names to variables or subgraphs.
    index_to_ref: A mapping from indexes to nodes that have been traversed.
      If a node is already in the cache, it won't be traversed again.
    index_ref_cache: A mapping from indexes to existing nodes that can be reused.
      When an reference is reused, ``GraphNodeImpl.clear`` is called to leave the
      object in an empty state and then filled by the unflatten process, as a result
      existing graph nodes are mutated to have the new content/topology
      specified by the nodedef.
  """
  if type(nodedef) is NodeRef:
    return index_ref[nodedef.index]

  if nodedef.index in index_ref:
    raise RuntimeError(f'GraphDef index {nodedef.index} already used.')

  def _get_children():
    children: list[tuple[Key, NodeLeaf | Node]] = []

    assert type(nodedef) is NodeDef
    for key, value in nodedef.attributes:
      if type(value) is Static:
        children.append((key, value.value))
      elif type(value) is NodeRef:
        children.append((key, index_ref[value.index]))
      elif type(value) is NodeDef:
        # if the key is a subgraph we create an empty node
        subgraphdef = value
        value_node_impl = get_node_impl_for_type(subgraphdef.type)
        assert value_node_impl is not None
        subnode = _graph_unflatten(
          subgraphdef, value_node_impl, leaves, index_ref, outer_index_outer_ref
        )
        children.append((key, subnode))
      elif type(value) is VariableDef:
        variabledef = value
        if not leaves:
          raise ValueError('Not enough leaves to unflatten the graph')
        # its a unseen variable, create a new one
        value = leaves.popleft()
        # when idxmap is present, check if the Varable exists there
        # and update existing variables if it does
        if (
          outer_index_outer_ref is not None
          and variabledef.outer_index in outer_index_outer_ref
        ):
          # if variable exists, update it
          variable = outer_index_outer_ref[variabledef.outer_index]
          if not isinstance(variable, Variable):
            raise ValueError(
              f'Expected a Variable type for {key!r}, but got {type(variable)}.'
            )
          if isinstance(value, VariableState):
            variable.update_from_state(value)
          else:
            variable.raw_value = value
        else:  # variabledef.index not in index_ref_cache
          # variable reference does not exist outside, create a new one
          if isinstance(value, VariableState):
            variable = value.to_variable()
          else:
            variable = variabledef.type.from_metadata(
              value, variabledef.metadata
            )
        children.append((key, variable))
        index_ref[variabledef.index] = variable
      else:
        raise RuntimeError(f'Unknown static field: {key!r}')

    return children

  if isinstance(node_impl, GraphNodeImpl):
    # we create an empty node first and add it to the index
    # this avoids infinite recursion when there is a reference cycle
    if (
      outer_index_outer_ref is not None
      and nodedef.outer_index in outer_index_outer_ref
    ):
      node = outer_index_outer_ref[nodedef.outer_index]
      if type(node) != nodedef.type:
        raise ValueError(
          f'Expected a node of type {nodedef.type} for index '
          f'{nodedef.index}, but got a node of type {type(node)}.'
        )
      node_impl.clear(node)
    else:
      node = node_impl.create_empty(nodedef.metadata)
    index_ref[nodedef.index] = node
    node_impl.init(node, _get_children())
  else:
    # if the node type does not support the creation of an empty object it means
    # that it cannot reference itself, so we can create its children first
    node = node_impl.unflatten(_get_children(), nodedef.metadata)

  return node


def graph_pop(
  node: tp.Any,
  filters: tuple[filterlib.Filter, ...],
) -> tuple[GraphState, ...]:
  id_to_index: dict[int, Index] = {}
  path_parts: PathParts = ()
  predicates = tuple(filterlib.to_predicate(filter) for filter in filters)
  flat_states: tuple[dict[PathParts, StateLeaf], ...] = tuple(
    {} for _ in predicates
  )
  _graph_pop(node, id_to_index, path_parts, flat_states, predicates)
  return tuple(
    GraphState.from_flat_path(flat_state) for flat_state in flat_states
  )


def _graph_pop(
  node: tp.Any,
  id_to_index: dict[int, Index],
  path_parts: PathParts,
  flat_states: tuple[dict[PathParts, StateLeaf], ...],
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
      _graph_pop(
        node=value,
        id_to_index=id_to_index,
        path_parts=(*path_parts, name),
        flat_states=flat_states,
        predicates=predicates,
      )
      continue
    elif not is_node_leaf(value):
      continue
    elif id(value) in id_to_index:
      continue

    node_path = (*path_parts, name)
    node_impl = get_node_impl(node)
    for state, predicate in zip(flat_states, predicates):
      if predicate(node_path, value):
        if isinstance(node_impl, PytreeNodeImpl):
          raise ValueError(
            f'Cannot pop key {name!r} from node of type {type(node).__name__}'
          )
        id_to_index[id(value)] = len(id_to_index)
        node_impl.pop_key(node, name)
        if isinstance(value, Variable):
          value = value.to_state()
        state[node_path] = value  # type: ignore[index] # mypy is wrong here?
        break
    else:
      # NOTE: should we raise an error here?
      pass


def _graph_update_dynamic(node: tp.Any, state: tp.Mapping[KeyT, tp.Any]):
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
      if is_state_leaf(value):
        raise ValueError(f'Expected a subgraph for {key!r}, but got: {value!r}')
      _graph_update_dynamic(current_value, value)
    else:
      # case 3: state leaf is being updated
      if not isinstance(current_value, Variable):
        raise ValueError(
          f'Trying to update a non-Variable attribute {key!r} with a Variable: '
          f'{value!r}'
        )
      if isinstance(value, VariableState):
        # updated from VariableState
        current_value.update_from_state(value)
      else:
        # updated from raw value
        current_value.raw_value = value


# --------------------------------------------------------
# UpdateContext
# --------------------------------------------------------


class CacheContext(tp.NamedTuple):
  fingerprint: tuple[tp.Any, ...]
  graphdef: GraphDef[tp.Any]
  final_graphdef: GraphDef[tp.Any]
  paths: tuple[PathParts, ...]
  variables: list[Variable[tp.Any]]
  new_ref_index: RefMap[tp.Any, Index]
  new_index_ref: dict[Index, tp.Any]

  @staticmethod
  def create(
    fingerprint: tuple[tp.Any, ...],
    graphdef: GraphDef[tp.Any],
    paths: tuple[PathParts, ...],
    variables: list[Variable[tp.Any]],
    new_ref_index: RefMap[tp.Any, Index],
  ) -> CacheContext:
    new_index_ref = {index: obj for obj, index in new_ref_index.items()}
    if type(graphdef) is NodeDef:
      final_graphdef = graphdef.with_same_outer_index()
    else:
      final_graphdef = graphdef
    return CacheContext(
      fingerprint,
      graphdef,
      final_graphdef,
      paths,
      variables,
      new_ref_index,
      new_index_ref,
    )


@dataclasses.dataclass
class GraphContext(threading.local):
  update_context_stacks: dict[str, list[UpdateContext]] = dataclasses.field(
    default_factory=dict
  )
  ref_index_stack: list[SplitContext] = dataclasses.field(default_factory=list)
  index_ref_stack: list[MergeContext] = dataclasses.field(default_factory=list)
  cache_context: WeakKeyDictionary[
    tp.Callable, WeakKeyDictionary[tp.Any, CacheContext]
  ] = dataclasses.field(default_factory=WeakKeyDictionary)


GRAPH_CONTEXT = GraphContext()


@dataclasses.dataclass
class SplitContext:
  ctxtag: str | None
  ref_index: RefMap[tp.Any, Index]

  @tp.overload
  def split(self, graph_node: A, /) -> tuple[GraphDef[A], GraphState]: ...
  @tp.overload
  def split(
    self, graph_node: A, first: filterlib.Filter, /
  ) -> tuple[GraphDef[A], GraphState]: ...
  @tp.overload
  def split(
    self,
    graph_node: A,
    first: filterlib.Filter,
    second: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
  ) -> tuple[GraphDef[A], GraphState, tpe.Unpack[tuple[GraphState, ...]]]: ...
  def split(
    self, node: A, *filters: filterlib.Filter
  ) -> tuple[GraphDef[A], tpe.Unpack[tuple[GraphState, ...]]]:
    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )
    inner_ref_outer_index = ctx and ctx.inner_ref_outer_index
    graphdef, flat_state = flatten(
      node, ref_index=self.ref_index, ref_outer_index=inner_ref_outer_index
    )
    flat_states = _split_state(flat_state, filters)
    states = tuple(
      State.from_flat_path(flat_state) for flat_state in flat_states
    )

    return graphdef, *states

  @tp.overload
  def flatten(
    self,
    graph_node: A,
    /,
    *,
    with_paths: tp.Literal[False],
    cache_context: WeakKeyDictionary[tp.Any, CacheContext] | None = None,
  ) -> tuple[GraphDef[A], list[tp.Any]]: ...
  @tp.overload
  def flatten(
    self,
    graph_node: A,
    /,
    *,
    cache_context: WeakKeyDictionary[tp.Any, CacheContext] | None = None,
  ) -> tuple[GraphDef[A], FlatState[VariableState[tp.Any]]]: ...
  @tp.overload
  def flatten(
    self,
    graph_node: A,
    first: filterlib.Filter,
    /,
    *,
    cache_context: WeakKeyDictionary[tp.Any, CacheContext] | None = None,
  ) -> tuple[GraphDef[A], FlatState[VariableState[tp.Any]]]: ...
  @tp.overload
  def flatten(
    self,
    graph_node: A,
    first: filterlib.Filter,
    second: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
    cache_context: WeakKeyDictionary[tp.Any, CacheContext] | None = None,
  ) -> tuple[
    GraphDef[A],
    FlatState[VariableState[tp.Any]],
    tpe.Unpack[tuple[FlatState[VariableState[tp.Any]], ...]],
  ]: ...
  def flatten(
    self,
    node: A,
    *filters: filterlib.Filter,
    with_paths: bool = True,
    cache_context: WeakKeyDictionary[tp.Any, CacheContext] | None = None,
  ) -> tuple[
    GraphDef[A],
    FlatState[VariableState[tp.Any]] | list[tp.Any],
    tpe.Unpack[tuple[FlatState[VariableState[tp.Any]], ...]],
  ]:
    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )
    ref_outer_index = ctx and ctx.inner_ref_outer_index

    def maybe_flatten() -> tuple[GraphDef[A], FlatState[tp.Any] | list[tp.Any]]:
      if cache_context is None:
        return flatten(
          node,
          ref_index=self.ref_index,
          with_paths=with_paths,
          ref_outer_index=ref_outer_index,
        )
      elif node in cache_context:
        node_cache = cache_context[node]
        cache_fp = node_cache.fingerprint
        new_ref_index = RefMap()
        node_fp = fingerprint(
          node, ref_index=self.ref_index, new_ref_index=new_ref_index
        )
        if cache_fp == node_fp:
          graphdef = node_cache.graphdef

          if with_paths:
            paths = node_cache.paths
            leaves = [variable.to_state() for variable in node_cache.variables]
          else:
            paths = None
            leaves = [variable.raw_value for variable in node_cache.variables]

          # add the new references to the ref_index
          self.ref_index.update(new_ref_index)
        else:
          graphdef, flat_states = flatten(
            node,
            ref_index=self.ref_index,
            ref_outer_index=ref_outer_index,
            with_paths=True,
            return_variables=True,
          )
          paths = flat_states.get_keys()
          variables = flat_states.get_values()
          assert paths is not None
          if with_paths:
            leaves = [variable.to_state() for variable in variables]
          else:
            leaves = [variable.raw_value for variable in variables]
          cache_context[node] = CacheContext.create(
            node_fp, graphdef, paths, variables, new_ref_index
          )
      else:  # node not in cache_context
        new_ref_index = RefMap()
        node_fp = fingerprint(
          node, ref_index=self.ref_index, new_ref_index=new_ref_index
        )
        graphdef, flat_state = flatten(
          node,
          ref_index=self.ref_index,
          ref_outer_index=ref_outer_index,
          with_paths=True,
          return_variables=True,
        )
        paths = flat_state.get_keys()
        variables = flat_state.get_values()
        if with_paths:
          leaves = [variable.to_state() for variable in variables]
        else:
          leaves = [variable.raw_value for variable in variables]
        cache_context[node] = CacheContext.create(
          node_fp, graphdef, paths, variables, new_ref_index
        )

      if with_paths:
        assert paths is not None
        return graphdef, FlatState.from_sorted_keys_values(paths, leaves)
      else:
        return graphdef, leaves

    if with_paths:
      graphdef, flat_state = maybe_flatten()
      flat_state = tp.cast(FlatState[VariableState[tp.Any]], flat_state)
      flat_states = _split_state(flat_state, filters)
    else:
      if filters:
        raise ValueError('Cannot use filters with with_paths=False')
      graphdef, flat_state = maybe_flatten()
      flat_state = tp.cast(list[tp.Any], flat_state)
      flat_states = (flat_state,)

    return graphdef, *flat_states  # type: ignore


@contextlib.contextmanager
def split_context(ctxtag: str | None = None):
  GRAPH_CONTEXT.ref_index_stack.append(SplitContext(ctxtag, RefMap()))

  try:
    yield GRAPH_CONTEXT.ref_index_stack[-1]
  finally:
    flatten_ctx = GRAPH_CONTEXT.ref_index_stack.pop()
    if ctxtag is not None:
      ctx = current_update_context(ctxtag)
      ctx.flatten_end(flatten_ctx.ref_index)
    del flatten_ctx.ref_index
    del flatten_ctx.ctxtag


@dataclasses.dataclass
class MergeContext:
  ctxtag: str | None
  index_ref: dict[Index, tp.Any]

  def merge(
    self,
    graphdef: GraphDef[A],
    state: GraphState,
    /,
    *states: GraphState,
  ) -> A:
    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )

    state = State.merge(state, *states)
    node = unflatten(
      graphdef,
      state,
      index_ref=self.index_ref,
      outer_index_outer_ref=ctx and ctx.outer_index_outer_ref,
    )
    return node

  def unflatten(
    self,
    graphdef: GraphDef[A],
    flat_state: GraphFlatState | list[tp.Any],
    /,
    *flat_states: GraphFlatState,
    cache_context: WeakKeyDictionary[tp.Any, CacheContext] | None = None,
  ) -> A:
    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )

    if type(flat_state) is list:
      if flat_states:
        raise ValueError(
          'Cannot use multiple flat_states when flat_state is a list, '
          f'got flat_state: {flat_state!r}, flat_states: {flat_states!r}'
        )
      state = flat_state
    else:
      state = FlatState.merge(flat_state, *flat_states)

    if type(graphdef) is NodeRef:
      node = unflatten(
        graphdef,
        state,
        index_ref=self.index_ref,
      )
    elif cache_context is None:
      node = unflatten(
        graphdef,
        state,
        index_ref=self.index_ref,
        outer_index_outer_ref=ctx and ctx.outer_index_outer_ref,
      )
    else:
      assert isinstance(graphdef, NodeDef)
      assert ctx is not None
      if (outer_index := graphdef.outer_index) is not None:
        outer_index_outer_ref = ctx.outer_index_outer_ref
        assert outer_index_outer_ref is not None
        node = outer_index_outer_ref[outer_index]
        if node in cache_context:
          # node is in cache_context, retrieve its cache
          cache = cache_context[node]
          # check if the graphdef is the same
          if cache.final_graphdef == graphdef:
            if type(state) is list:
              leaves = state
            elif type(state) is FlatState:  # type: ignore
              leaves = state.get_values()
            else:
              raise ValueError(f'Unsupported state type: {type(state)}')

            # graphdefs match, update variables from state
            if len(leaves) != len(cache.variables):
              raise ValueError(
                f'Incorrect number of leaves: expected {len(cache.variables)} '
                f'leaves in the state, got {len(leaves)}'
              )
            for variable, leaf in zip(cache.variables, leaves):
              if type(leaf) is VariableState:
                variable.update_from_state(leaf)
              else:
                variable.raw_value = leaf
            self.index_ref.update(cache.new_index_ref)
          else:  # cache.graphdef != graphdef_fp
            # graph changed, re-create the node
            node = unflatten(
              graphdef,
              state,
              index_ref=self.index_ref,
              outer_index_outer_ref=outer_index_outer_ref,
            )
        else:  # node not in cache_context
          # all nodes in index_ref_cache must be in cache_context
          raise RuntimeError(f'Node not found in cache_context, node: {node}')
      else:  # graphdef.outer_index is None
        # its a new node, create it
        node = unflatten(
          graphdef,
          state,
          index_ref=self.index_ref,
        )

    return node

@tp.overload
@contextlib.contextmanager
def merge_context(): ...
@tp.overload
@contextlib.contextmanager
def merge_context(inner: bool | None, ctxtag: str | None): ...
@contextlib.contextmanager
def merge_context(inner: bool | None = None, ctxtag: str | None = None):
  GRAPH_CONTEXT.index_ref_stack.append(MergeContext(ctxtag, {}))

  try:
    yield GRAPH_CONTEXT.index_ref_stack[-1]
  finally:
    unflatten_ctx = GRAPH_CONTEXT.index_ref_stack.pop()
    index_ref = unflatten_ctx.index_ref
    if ctxtag is not None:
      if inner is None:
        raise ValueError('inner_merge must be specified when using ctxtag')
      ctx = current_update_context(ctxtag)
      ctx.unflatten_end(index_ref, inner)
    del unflatten_ctx.index_ref
    del unflatten_ctx.ctxtag


@dataclasses.dataclass
class UpdateContext:
  """A context manager for handling complex state updates."""

  tag: str
  outer_ref_outer_index: RefMap[tp.Any, Index] | None
  outer_index_inner_ref: dict[Index, tp.Any] | None
  # reverse caches
  outer_index_outer_ref: dict[Index, tp.Any] | None
  inner_ref_outer_index: RefMap[tp.Any, Index] | None

  # define hash and eq to make this an opaque object
  def __hash__(self):
    return 0

  def __eq__(self, other):
    return isinstance(other, UpdateContext)

  def flatten_end(self, ref_index: RefMap[tp.Any, Index]):
    if self.outer_ref_outer_index is None:
      # outer split (1), store the references
      self.outer_ref_outer_index = ref_index
      self.outer_index_outer_ref = {
        index: obj for obj, index in self.outer_ref_outer_index.items()
      }
    else:
      # inner split (3), clear index_ref
      self.outer_index_inner_ref = None
      self.inner_ref_outer_index = None

  def unflatten_end(self, index_ref: dict[Index, tp.Any], inner_merge: bool):
    if inner_merge:
      # inner merge (2)
      self.outer_index_inner_ref = index_ref
      self.inner_ref_outer_index = RefMap(
        (obj, index) for index, obj in index_ref.items()
      )

  @tp.overload
  def split(self, graph_node: A, /) -> tuple[GraphDef[A], GraphState]: ...
  @tp.overload
  def split(
    self, graph_node: A, first: filterlib.Filter, /
  ) -> tuple[GraphDef[A], GraphState]: ...
  @tp.overload
  def split(
    self,
    graph_node: A,
    first: filterlib.Filter,
    second: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
  ) -> tuple[GraphDef[A], GraphState, tpe.Unpack[tuple[GraphState, ...]]]: ...
  def split(
    self, node: A, *filters: filterlib.Filter
  ) -> tuple[GraphDef[A], GraphState, tpe.Unpack[tuple[GraphState, ...]]]:
    """Split a graph node into a :class:`GraphDef` and one or more :class:`State`s. State is
    a ``Mapping`` from strings or integers to ``Variables``, Arrays or nested States. GraphDef
    contains all the static information needed to reconstruct a ``Module`` graph, it is analogous
    to JAX’s ``PyTreeDef``. :func:`split` is used in conjunction with :func:`merge` to switch
    seamlessly between stateful and stateless representations of the graph.

    Example usage::

      >>> from flax import nnx
      >>> import jax, jax.numpy as jnp
      ...
      >>> class Foo(nnx.Module):
      ...   def __init__(self, rngs):
      ...     self.batch_norm = nnx.BatchNorm(2, rngs=rngs)
      ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
      ...
      >>> node = Foo(nnx.Rngs(0))
      >>> graphdef, params, batch_stats = nnx.split(node, nnx.Param, nnx.BatchStat)
      ...
      >>> jax.tree.map(jnp.shape, params)
      State({
        'batch_norm': {
          'bias': VariableState(
            type=Param,
            value=(2,)
          ),
          'scale': VariableState(
            type=Param,
            value=(2,)
          )
        },
        'linear': {
          'bias': VariableState(
            type=Param,
            value=(3,)
          ),
          'kernel': VariableState(
            type=Param,
            value=(2, 3)
          )
        }
      })
      >>> jax.tree.map(jnp.shape, batch_stats)
      State({
        'batch_norm': {
          'mean': VariableState(
            type=BatchStat,
            value=(2,)
          ),
          'var': VariableState(
            type=BatchStat,
            value=(2,)
          )
        }
      })

    Arguments:
      node: graph node to split.
      *filters: some optional filters to group the state into mutually exclusive substates.
    Returns:
      :class:`GraphDef` and one or more :class:`State`'s equal to the number of filters passed. If no
      filters are passed, a single :class:`State` is returned.
    """
    ref_index: RefMap[tp.Any, Index] = RefMap()
    graphdef, flat_state = flatten(
      node, ref_index=ref_index, ref_outer_index=self.inner_ref_outer_index
    )
    states = tuple(
      State.from_flat_path(flat_state)
      for flat_state in _split_state(flat_state, filters)
    )

    self.flatten_end(ref_index)

    return graphdef, *states

  def merge(
    self,
    graphdef: GraphDef[A],
    state: GraphState,
    *states: GraphState,
  ) -> A:
    """merge"""
    if not isinstance(graphdef, NodeDef):
      raise ValueError(
        f'Expected a NodeDef instance, but got {type(graphdef)}.'
      )
    if self.outer_ref_outer_index is None:
      raise ValueError('Cannot merge without ref_index.')

    if self.outer_ref_outer_index is not None:
      # outer merge (4), create index_ref_cache
      index_ref_cache = self.outer_index_outer_ref
      assert index_ref_cache is not None
    else:
      # inner merge (2)
      index_ref_cache = None

    state = State.merge(state, *states)
    index_ref: dict[Index, tp.Any] = {}
    node = unflatten(
      graphdef,
      state,
      index_ref=index_ref,
      outer_index_outer_ref=index_ref_cache,
    )

    self.unflatten_end(index_ref, True)

    return node


jax.tree_util.register_static(UpdateContext)


@dataclasses.dataclass
class UpdateContextManager:
  tag: str

  def __enter__(self):
    ctx = UpdateContext(self.tag, None, None, None, None)
    if self.tag not in GRAPH_CONTEXT.update_context_stacks:
      GRAPH_CONTEXT.update_context_stacks[self.tag] = [ctx]
    else:
      GRAPH_CONTEXT.update_context_stacks[self.tag].append(ctx)
    return ctx

  def __exit__(self, *args):
    if self.tag not in GRAPH_CONTEXT.update_context_stacks:
      raise RuntimeError(
          f'No update context found for tag {self.tag!r}, this is a bug.'
      )
    stack = GRAPH_CONTEXT.update_context_stacks[self.tag]

    ctx = stack.pop()
    # clear references
    del ctx.outer_ref_outer_index
    del ctx.outer_index_inner_ref
    del ctx.outer_index_outer_ref
    del ctx.inner_ref_outer_index

    if not stack:
      del GRAPH_CONTEXT.update_context_stacks[self.tag]

  def __call__(self, f: F) -> F:
    @functools.wraps(f)
    def update_context_manager_wrapper(*args, **kwargs):
      with self:
        return f(*args, **kwargs)

    return update_context_manager_wrapper  # type: ignore


def update_context(tag: str):
  """Creates an :class:`UpdateContext` context manager which can be used to handle
  more complex state updates beyond what ``nnx.update`` can handle, including
  updates to static properties and graph structure.

  UpdateContext exposes a ``split`` and ``merge`` API with the same
  signature as ``nnx.split`` / ``nnx.merge`` but performs some bookkeeping
  to have the necessary information in order to perfectly update the input
  objects based on the changes made inside the transform. The UpdateContext
  must call split and merge a total of 4 times, the first
  and last calls happen outside the transform and the second and third calls
  happen inside the transform as shown in the diagram below::


                          idxmap
    (2) merge ─────────────────────────────► split (3)
          ▲                                    │
          │               inside               │
          │. . . . . . . . . . . . . . . . . . │ index_mapping
          │               outside              │
          │                                    ▼
    (1) split──────────────────────────────► merge (4)
                          refmap


  The first call to split ``(1)`` creates a ``refmap`` which keeps track of the
  outer references, and the first call to merge ``(2)`` creates an ``idxmap`` which
  keeps track of the inner references. The second call to split ``(3)`` combines
  the refmap and idxmap to produce the ``index_mapping`` which indicates
  how the outer references map to the inner references. Finally, the last call to
  merge ``(4)`` uses the index_mapping and the refmap to reconstruct the
  output of the transform while reusing/updating the inner references. To avoid
  memory leaks, the idxmap is cleared after ``(3)`` and the refmap is
  cleared after ``(4)``, and both are cleared after the context manager exits.

  Here is a simple example showing the use of ``update_context``::

    >>> from flax import nnx
    ...
    >>> m1 = nnx.Dict({})
    >>> with nnx.update_context('example') as ctx:
    ...   graphdef, state = ctx.split(m1)
    ...   @jax.jit
    ...   def f(graphdef, state):
    ...     m2 = ctx.merge(graphdef, state)
    ...     m2.a = 1
    ...     m2.ref = m2  # create a reference cycle
    ...     return ctx.split(m2)
    ...   graphdef_out, state_out = f(graphdef, state)
    ...   m3 = ctx.merge(graphdef_out, state_out)
    ...
    >>> assert m1 is m3
    >>> assert m1.a == 1
    >>> assert m1.ref is m1

  Note that ``update_context`` takes in a ``tag`` argument which is used
  primarily as a safety mechanism reduce the risk of accidentally using the
  wrong UpdateContext when using :func:`current_update_context` to access the
  current active context. current_update_context can be used as a way of
  accessing the current active context without having to pass it as a capture::

    >>> from flax import nnx
    ...
    >>> m1 = nnx.Dict({})
    >>> @jax.jit
    ... def f(graphdef, state):
    ...   ctx = nnx.current_update_context('example')
    ...   m2 = ctx.merge(graphdef, state)
    ...   m2.a = 1     # insert static attribute
    ...   m2.ref = m2  # create a reference cycle
    ...   return ctx.split(m2)
    ...
    >>> @nnx.update_context('example')
    ... def g(m1):
    ...   ctx = nnx.current_update_context('example')
    ...   graphdef, state = ctx.split(m1)
    ...   graphdef_out, state_out = f(graphdef, state)
    ...   return ctx.merge(graphdef_out, state_out)
    ...
    >>> m3 = g(m1)
    >>> assert m1 is m3
    >>> assert m1.a == 1
    >>> assert m1.ref is m1

  As shown in the code above, ``update_context`` can also be used as a
  decorator that creates/activates an UpdateContext context for the
  duration of the function. The context can be accessed using
  :func:`current_update_context`.

  Args:
    tag: A string tag to identify the context.
  """
  return UpdateContextManager(tag)


def current_update_context(tag: str) -> UpdateContext:
  """Returns the current active :class:`UpdateContext` for the given tag."""
  if tag not in GRAPH_CONTEXT.update_context_stacks:
    raise ValueError(f'No update context found for tag {tag!r}.')
  return GRAPH_CONTEXT.update_context_stacks[tag][-1]


# --------------------------------------------------------
# Functional API
# --------------------------------------------------------

def _split_state(
  state: FlatState[tp.Any],
  filters: tuple[filterlib.Filter, ...],
) -> tuple[FlatState[tp.Any], tpe.Unpack[tuple[FlatState[tp.Any], ...]]]:
  if not filters:
    return (state,)
  states = state.split(*filters)
  if not isinstance(states, tuple):
    return (states,)
  assert len(states) > 0
  return states  # type: ignore[return-value]


@tp.overload
def split(graph_node: A, /) -> tuple[GraphDef[A], GraphState]: ...
@tp.overload
def split(
  graph_node: A, first: filterlib.Filter, /
) -> tuple[GraphDef[A], GraphState]: ...
@tp.overload
def split(
  graph_node: A,
  first: filterlib.Filter,
  second: filterlib.Filter,
  /,
  *filters: filterlib.Filter,
) -> tuple[GraphDef[A], GraphState, tpe.Unpack[tuple[GraphState, ...]]]: ...
def split(
  node: A, *filters: filterlib.Filter
) -> tuple[GraphDef[A], GraphState, tpe.Unpack[tuple[GraphState, ...]]]:
  """Split a graph node into a :class:`GraphDef` and one or more :class:`State`s. State is
  a ``Mapping`` from strings or integers to ``Variables``, Arrays or nested States. GraphDef
  contains all the static information needed to reconstruct a ``Module`` graph, it is analogous
  to JAX’s ``PyTreeDef``. :func:`split` is used in conjunction with :func:`merge` to switch
  seamlessly between stateful and stateless representations of the graph.

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp
    ...
    >>> class Foo(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.batch_norm = nnx.BatchNorm(2, rngs=rngs)
    ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
    ...
    >>> node = Foo(nnx.Rngs(0))
    >>> graphdef, params, batch_stats = nnx.split(node, nnx.Param, nnx.BatchStat)
    ...
    >>> jax.tree.map(jnp.shape, params)
    State({
      'batch_norm': {
        'bias': VariableState(
          type=Param,
          value=(2,)
        ),
        'scale': VariableState(
          type=Param,
          value=(2,)
        )
      },
      'linear': {
        'bias': VariableState(
          type=Param,
          value=(3,)
        ),
        'kernel': VariableState(
          type=Param,
          value=(2, 3)
        )
      }
    })
    >>> jax.tree.map(jnp.shape, batch_stats)
    State({
      'batch_norm': {
        'mean': VariableState(
          type=BatchStat,
          value=(2,)
        ),
        'var': VariableState(
          type=BatchStat,
          value=(2,)
        )
      }
    })

  :func:`split` and :func:`merge` are primarily used to interact directly with JAX
  transformations, see
  `Functional API <https://flax.readthedocs.io/en/latest/nnx/nnx_basics.html#the-functional-api>`__
  for more information.

  Arguments:
    node: graph node to split.
    *filters: some optional filters to group the state into mutually exclusive substates.
  Returns:
    ``GraphDef`` and one or more ``States`` equal to the number of filters passed. If no
    filters are passed, a single ``State`` is returned.
  """
  graphdef, flat_state = flatten(node)
  flat_states = _split_state(flat_state, filters)
  states = tuple(State.from_flat_path(flat_state) for flat_state in flat_states)
  return graphdef, *states  # type: ignore[return-value]


def merge(
  graphdef: GraphDef[A],
  state: tp.Mapping[KeyT, tp.Any],
  /,
  *states: tp.Mapping[KeyT, tp.Any],
) -> A:
  """The inverse of :func:`flax.nnx.split`.

  ``nnx.merge`` takes a :class:`flax.nnx.GraphDef` and one or more :class:`flax.nnx.State`'s
  and creates a new node with the same structure as the original node.

  Recall: :func:`flax.nnx.split` is used to represent a :class:`flax.nnx.Module`
  by: 1) a static ``nnx.GraphDef`` that captures its Pythonic static information;
  and 2) one or more :class:`flax.nnx.Variable` ``nnx.State``'(s) that capture
  its ``jax.Array``'s in the form of JAX pytrees.

  ``nnx.merge`` is used in conjunction with ``nnx.split`` to switch seamlessly
  between stateful and stateless representations of the graph.

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp
    ...
    >>> class Foo(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.batch_norm = nnx.BatchNorm(2, rngs=rngs)
    ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
    ...
    >>> node = Foo(nnx.Rngs(0))
    >>> graphdef, params, batch_stats = nnx.split(node, nnx.Param, nnx.BatchStat)
    ...
    >>> new_node = nnx.merge(graphdef, params, batch_stats)
    >>> assert isinstance(new_node, Foo)
    >>> assert isinstance(new_node.batch_norm, nnx.BatchNorm)
    >>> assert isinstance(new_node.linear, nnx.Linear)

  ``nnx.split`` and ``nnx.merge`` are primarily used to interact directly with JAX
  transformations (refer to
  `Functional API <https://flax.readthedocs.io/en/latest/nnx_basics.html#the-flax-functional-api>`__
  for more information.

  Args:
    graphdef: A :class:`flax.nnx.GraphDef` object.
    state: A :class:`flax.nnx.State` object.
    *states: Additional :class:`flax.nnx.State` objects.
  Returns:
    The merged :class:`flax.nnx.Module`.
  """
  state = State.merge(state, *states)
  node = unflatten(graphdef, state)
  return node


def update(
  node, state: tp.Mapping[KeyT, tp.Any], /, *states: tp.Mapping[KeyT, tp.Any]
) -> None:
  """Update the given graph node with a new state(s) in-place.

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp

    >>> x = jnp.ones((1, 2))
    >>> y = jnp.ones((1, 3))
    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))

    >>> def loss_fn(model, x, y):
    ...   return jnp.mean((y - model(x))**2)
    >>> prev_loss = loss_fn(model, x, y)

    >>> grads = nnx.grad(loss_fn)(model, x, y)
    >>> new_state = jax.tree.map(lambda p, g: p - 0.1*g, nnx.state(model), grads)
    >>> nnx.update(model, new_state)
    >>> assert loss_fn(model, x, y) < prev_loss

  Args:
    node: A graph node to update.
    state: A :class:`State` object.
    *states: Additional :class:`State` objects.
  """
  if states:
    state = State.merge(state, *states)
  if isinstance(state, State):
    state = state.raw_mapping
  _graph_update_dynamic(node, state)

def _variables_generator(node) -> tp.Iterable[tuple[PathParts, Variable]]:
  for path, value in iter_graph(node):
    if isinstance(value, Variable):
      yield path, value


@tp.overload
def variables(node, /) -> State[Key, Variable]: ...
@tp.overload
def variables(node, first: filterlib.Filter, /) -> State[Key, Variable]: ...
@tp.overload
def variables(
  node,
  first: filterlib.Filter,
  second: filterlib.Filter,
  /,
  *filters: filterlib.Filter,
) -> tuple[State[Key, Variable], ...]: ...
def variables(
  node,
  *filters: filterlib.Filter,
) -> tp.Union[State[Key, Variable], tuple[State[Key, Variable], ...]]:
  """Similar to :func:`state` but returns the current :class:`Variable` objects instead
  of new :class:`VariableState` instances.

  Example::

    >>> from flax import nnx
    ...
    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> params = nnx.variables(model, nnx.Param)
    ...
    >>> assert params['kernel'] is model.kernel
    >>> assert params['bias'] is model.bias

  Args:
    node: A graph node object.
    *filters: One or more :class:`Variable` objects to filter by.
  Returns:
    One or more :class:`State` mappings containing the :class:`Variable` objects.
  """
  num_filters = len(filters)
  if num_filters == 0:
    filters = (..., ...)
  else:
    filters = (*filters, ...)

  variables_iterable = _variables_generator(node)
  flat_states = variablelib.split_flat_state(
    variables_iterable, (*filters, ...)
  )
  states = tuple(State.from_flat_path(flat_state) for flat_state in flat_states)
  if num_filters < 2:
    return states[0]
  return states

@tp.overload
def state(node, /) -> GraphState: ...
@tp.overload
def state(node, first: filterlib.Filter, /) -> GraphState: ...
@tp.overload
def state(
  node,
  first: filterlib.Filter,
  second: filterlib.Filter,
  /,
  *filters: filterlib.Filter,
) -> tuple[GraphState, ...]: ...
def state(
  node,
  *filters: filterlib.Filter,
) -> tp.Union[GraphState, tuple[GraphState, ...]]:
  """Similar to :func:`split` but only returns the :class:`State`'s indicated by the filters.

  Example usage::

    >>> from flax import nnx

    >>> class Model(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.batch_norm = nnx.BatchNorm(2, rngs=rngs)
    ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
    ...   def __call__(self, x):
    ...     return self.linear(self.batch_norm(x))

    >>> model = Model(rngs=nnx.Rngs(0))
    >>> # get the learnable parameters from the batch norm and linear layer
    >>> params = nnx.state(model, nnx.Param)
    >>> # get the batch statistics from the batch norm layer
    >>> batch_stats = nnx.state(model, nnx.BatchStat)
    >>> # get them separately
    >>> params, batch_stats = nnx.state(model, nnx.Param, nnx.BatchStat)
    >>> # get them together
    >>> state = nnx.state(model)

  Args:
    node: A graph node object.
    *filters: One or more :class:`Variable` objects to filter by.
  Returns:
    One or more :class:`State` mappings.
  """
  _, state = flatten(node)
  state = state.to_nested_state()

  states: GraphState | tuple[GraphState, ...]
  if len(filters) == 0:
    states = state
  elif len(filters) == 1:
    states = state.filter(filters[0])
  else:
    states = state.filter(filters[0], filters[1], *filters[2:])

  return states


def graphdef(node: tp.Any, /) -> GraphDef[tp.Any]:
  """Get the :class:`GraphDef` of the given graph node.

  Example usage::

    >>> from flax import nnx

    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> graphdef, _ = nnx.split(model)
    >>> assert graphdef == nnx.graphdef(model)

  Args:
    node: A graph node object.
  Returns:
    The :class:`GraphDef` of the :class:`Module` object.
  """
  graphdef, _ = flatten(node)
  return graphdef


@tp.overload
def pop(
  node,
  filter: filterlib.Filter,
  /,
) -> GraphState: ...


@tp.overload
def pop(
  node,
  filter: filterlib.Filter,
  filter2: filterlib.Filter,
  /,
  *filters: filterlib.Filter,
) -> tuple[GraphState, ...]: ...


def pop(
  node, *filters: filterlib.Filter
) -> tp.Union[GraphState, tuple[GraphState, ...]]:
  """Pop one or more :class:`Variable` types from the graph node.

  Example usage::

    >>> from flax import nnx
    >>> import jax.numpy as jnp

    >>> class Model(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.linear1 = nnx.Linear(2, 3, rngs=rngs)
    ...     self.linear2 = nnx.Linear(3, 4, rngs=rngs)
    ...   def __call__(self, x):
    ...     x = self.linear1(x)
    ...     self.sow(nnx.Intermediate, 'i', x)
    ...     x = self.linear2(x)
    ...     return x

    >>> x = jnp.ones((1, 2))
    >>> model = Model(rngs=nnx.Rngs(0))
    >>> assert not hasattr(model, 'i')
    >>> y = model(x)
    >>> assert hasattr(model, 'i')

    >>> intermediates = nnx.pop(model, nnx.Intermediate)
    >>> assert intermediates['i'].value[0].shape == (1, 3)
    >>> assert not hasattr(model, 'i')

  Args:
    node: A graph node object.
    *filters: One or more :class:`Variable` objects to filter by.
  Returns:
    The popped :class:`State` containing the :class:`Variable`
    objects that were filtered for.
  """
  if len(filters) == 0:
    raise ValueError('Expected at least one filter')

  id_to_index: dict[int, Index] = {}
  path_parts: PathParts = ()
  predicates = tuple(filterlib.to_predicate(filter) for filter in filters)
  flat_states: tuple[dict[PathParts, StateLeaf], ...] = tuple(
    {} for _ in predicates
  )
  _graph_pop(
    node=node,
    id_to_index=id_to_index,
    path_parts=path_parts,
    flat_states=flat_states,
    predicates=predicates,
  )
  states = tuple(
    GraphState.from_flat_path(flat_state) for flat_state in flat_states
  )

  if len(states) == 1:
    return states[0]
  else:
    return states


def clone(node: Node) -> Node:
  """Create a deep copy of the given graph node.

  Example usage::

    >>> from flax import nnx

    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> cloned_model = nnx.clone(model)
    >>> model.bias.value += 1
    >>> assert (model.bias.value != cloned_model.bias.value).all()

  Args:
    node: A graph node object.
  Returns:
    A deep copy of the :class:`Module` object.
  """
  graphdef, state = split(node)
  return merge(graphdef, state)


def call(
  graphdef_state: tuple[GraphDef[A], GraphState], /
) -> ApplyCaller[tuple[GraphDef[A], GraphState]]:
  """Calls a method underlying graph node defined by a (GraphDef, State) pair.

  ``call`` takes a ``(GraphDef, State)`` pair and creates a proxy object that can be
  used to call methods on the underlying graph node. When a method is called, the
  output is returned along with a new (GraphDef, State) pair that represents the
  updated state of the graph node. ``call`` is equivalent to :func:`merge` > ``method``
  > :func:`split`` but is more convenient to use in pure JAX functions.

  Example::

    >>> from flax import nnx
    >>> import jax
    >>> import jax.numpy as jnp
    ...
    >>> class StatefulLinear(nnx.Module):
    ...   def __init__(self, din, dout, rngs):
    ...     self.w = nnx.Param(jax.random.uniform(rngs(), (din, dout)))
    ...     self.b = nnx.Param(jnp.zeros((dout,)))
    ...     self.count = nnx.Variable(jnp.array(0, dtype=jnp.uint32))
    ...
    ...   def increment(self):
    ...     self.count += 1
    ...
    ...   def __call__(self, x):
    ...     self.increment()
    ...     return x @ self.w + self.b
    ...
    >>> linear = StatefulLinear(3, 2, nnx.Rngs(0))
    >>> linear_state = nnx.split(linear)
    ...
    >>> @jax.jit
    ... def forward(x, linear_state):
    ...   y, linear_state = nnx.call(linear_state)(x)
    ...   return y, linear_state
    ...
    >>> x = jnp.ones((1, 3))
    >>> y, linear_state = forward(x, linear_state)
    >>> y, linear_state = forward(x, linear_state)
    ...
    >>> linear = nnx.merge(*linear_state)
    >>> linear.count.value
    Array(2, dtype=uint32)

  The proxy object returned by ``call`` supports indexing and attribute access
  to access nested methods. In the example below, the ``increment`` method indexing
  is used to call the ``increment`` method of the ``StatefulLinear`` module
  at the ``b`` key of a ``nodes`` dictionary.

    >>> class StatefulLinear(nnx.Module):
    ...   def __init__(self, din, dout, rngs):
    ...     self.w = nnx.Param(jax.random.uniform(rngs(), (din, dout)))
    ...     self.b = nnx.Param(jnp.zeros((dout,)))
    ...     self.count = nnx.Variable(jnp.array(0, dtype=jnp.uint32))
    ...
    ...   def increment(self):
    ...     self.count += 1
    ...
    ...   def __call__(self, x):
    ...     self.increment()
    ...     return x @ self.w + self.b
    ...
    >>> rngs = nnx.Rngs(0)
    >>> nodes = dict(
    ...   a=StatefulLinear(3, 2, rngs),
    ...   b=StatefulLinear(2, 1, rngs),
    ... )
    ...
    >>> node_state = nnx.split(nodes)
    >>> # use attribute access
    >>> _, node_state = nnx.call(node_state)['b'].increment()
    ...
    >>> nodes = nnx.merge(*node_state)
    >>> nodes['a'].count.value
    Array(0, dtype=uint32)
    >>> nodes['b'].count.value
    Array(1, dtype=uint32)
  """

  def pure_caller(accessor: DelayedAccessor, *args, **kwargs):
    node = merge(*graphdef_state)
    method = accessor(node)
    out = method(*args, **kwargs)
    return out, split(node)

  return CallableProxy(pure_caller)  # type: ignore


def iter_graph(node: tp.Any, /) -> tp.Iterator[tuple[PathParts, tp.Any]]:
  """Iterates over all nested nodes and leaves of the given graph node, including the current node.

  ``iter_graph`` creates a generator that yields path and value pairs, where
  the path is a tuple of strings or integers representing the path to the value from the
  root. Repeated nodes are visited only once. Leaves include static values.

  Example::
    >>> from flax import nnx
    >>> import jax.numpy as jnp
    ...
    >>> class Linear(nnx.Module):
    ...   def __init__(self, din, dout, *, rngs: nnx.Rngs):
    ...     self.din, self.dout = din, dout
    ...     self.w = nnx.Param(jax.random.uniform(rngs.next(), (din, dout)))
    ...     self.b = nnx.Param(jnp.zeros((dout,)))
    ...
    >>> module = Linear(3, 4, rngs=nnx.Rngs(0))
    >>> graph = [module, module]
    ...
    >>> for path, value in nnx.iter_graph(graph):
    ...   print(path, type(value).__name__)
    ...
    (0, 'b') Param
    (0, 'din') int
    (0, 'dout') int
    (0, 'w') Param
    (0,) Linear
    () list
  """
  visited: set[int] = set()
  path_parts: PathParts = ()
  yield from _iter_graph(node, visited, path_parts)


def _iter_graph(
  node: tp.Any, visited: set[int], path_parts: PathParts
) -> tp.Iterator[tuple[PathParts, tp.Any]]:
  if is_node(node):
    if id(node) in visited:
      return
    visited.add(id(node))
    node_dict = get_node_impl(node).node_dict(node)
    for key, value in node_dict.items():
      yield from _iter_graph(value, visited, (*path_parts, key))

  yield path_parts, node




@dataclasses.dataclass(frozen=True)
class Static(tp.Generic[A]):
  """An empty pytree node that treats its inner value as static.
  ``value`` must define ``__eq__`` and ``__hash__``.
  """

  value: A


jax.tree_util.register_static(Static)

# ---------------------------------------------------------
# Pytree
# ---------------------------------------------------------
class GenericPytree: ...

from jax._src.tree_util import _registry as JAX_PYTREE_REGISTRY

def is_pytree_node(x: tp.Any) -> bool:
  if type(x) in JAX_PYTREE_REGISTRY:
    return True
  elif isinstance(x, tuple):
    return True
  else:
    return False


def _key_path_to_key(key: tp.Any) -> Key:
  if isinstance(key, jax.tree_util.SequenceKey):
    return key.idx
  elif isinstance(
    key, (jax.tree_util.DictKey, jax.tree_util.FlattenedIndexKey)
  ):
    if not is_key_like(key.key):
      raise ValueError(
        f'Invalid key: {key.key}. May be due to its type not being hashable or comparable.'
      )
    return key.key
  elif isinstance(key, jax.tree_util.GetAttrKey):
    return key.name
  else:
    return str(key)

class IndexesPytreeDef(tp.NamedTuple):
  key_index: HashableMapping[Key, int]
  treedef: jax.tree_util.PyTreeDef

def _flatten_pytree(pytree: tp.Any):
  leaves, treedef = jax.tree_util.tree_flatten_with_path(
    pytree, is_leaf=lambda x: x is not pytree
  )
  nodes = [(_key_path_to_key(path[0]), value) for path, value in leaves]
  key_index = HashableMapping(
    {key: i for i, (key, _) in enumerate(nodes)}, copy=False
  )
  nodes.sort()  # sort by key
  return nodes, IndexesPytreeDef(key_index, treedef)


def _unflatten_pytree(
  nodes: tuple[tuple[Key, tp.Any], ...], metadata: IndexesPytreeDef
):
  # sort to original order
  sorted_nodes = sorted(nodes, key=lambda x: metadata.key_index[x[0]])
  pytree = metadata.treedef.unflatten(value for _, value in sorted_nodes)
  return pytree


PYTREE_NODE_IMPL = PytreeNodeImpl(
  type=GenericPytree,
  flatten=_flatten_pytree,
  unflatten=_unflatten_pytree,  # type: ignore
)

# common pytrees
# list
register_pytree_node_type(
  list,
  flatten=lambda x: (list(enumerate(x)), None),
  unflatten=lambda nodes, _: [value for _, value in nodes],  # type: ignore
)
# tuple
register_pytree_node_type(
  tuple,
  flatten=lambda x: (list(enumerate(x)), None),
  unflatten=lambda nodes, _: tuple(value for _, value in nodes),  # type: ignore
)
# dict
register_pytree_node_type(
  dict,
  flatten=lambda x: (sorted(x.items()), None),
  unflatten=lambda nodes, _: {key: value for key, value in nodes},  # type: ignore
)
# None
register_pytree_node_type(
  type(None),
  flatten=lambda x: ([], None),
  unflatten=lambda _, __: None,  # type: ignore
)
