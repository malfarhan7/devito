from collections.abc import Iterable
from functools import singledispatch

from devito.symbolics import (retrieve_indexed, uxreplace, retrieve_dimensions,
                              retrieve_functions)
from devito.tools import Ordering, as_tuple, flatten, filter_sorted, filter_ordered
from devito.types import (Dimension, Eq, IgnoreDimSort, SubDimension,
                          ConditionalDimension)
from devito.types.array import Array
from devito.types.basic import AbstractFunction
from devito.types.dimension import MultiSubDimension

__all__ = ['dimension_sort', 'lower_exprs', 'concretize_subdims']


def dimension_sort(expr):
    """
    Topologically sort the Dimensions in ``expr``, based on the order in which they
    appear within Indexeds.
    """

    def handle_indexed(indexed):
        relation = []
        for i in indexed.indices:
            try:
                # Assume it's an AffineIndexAccessFunction...
                relation.append(i.d)
            except AttributeError:
                # It's not! Maybe there are some nested Indexeds (e.g., the
                # situation is A[B[i]])
                nested = flatten(handle_indexed(n) for n in retrieve_indexed(i))
                if nested:
                    relation.extend(nested)
                    continue

                # Fallback: Just insert all the Dimensions we find, regardless of
                # what the user is attempting to do
                relation.extend(filter_sorted(i.atoms(Dimension)))

        # StencilDimensions are lowered subsequently through special compiler
        # passes, so they can be ignored here
        relation = tuple(d for d in relation if not d.is_Stencil)

        return relation

    if isinstance(expr.implicit_dims, IgnoreDimSort):
        relations = set()
    else:
        relations = {handle_indexed(i) for i in retrieve_indexed(expr)}

    # Add in any implicit dimension (typical of scalar temporaries, or Step)
    relations.add(expr.implicit_dims)

    # Add in leftover free dimensions (not an Indexed' index)
    extra = set(retrieve_dimensions(expr, deep=True))

    # Add in pure data dimensions (e.g., those accessed only via explicit values,
    # such as A[3])
    indexeds = retrieve_indexed(expr, deep=True)
    for i in indexeds:
        extra.update({d for d in i.function.dimensions if i.indices[d].is_integer})

    # Enforce determinism
    extra = filter_sorted(extra)

    # Add in implicit relations for parent dimensions
    # -----------------------------------------------
    # 1) Note that (d.parent, d) is what we want, while (d, d.parent) would be
    # wrong; for example, in `((t, time), (t, x, y), (x, y))`, `x` could now
    # preceed `time`, while `t`, and therefore `time`, *must* appear before `x`,
    # as indicated by the second relation
    implicit_relations = {(d.parent, d) for d in extra if d.is_Derived and not d.indirect}

    # 2) To handle cases such as `((time, xi), (x,))`, where `xi` a SubDimension
    # of `x`, besides `(x, xi)`, we also have to add `(time, x)` so that we
    # obtain the desired ordering `(time, x, xi)`. W/o `(time, x)`, the ordering
    # `(x, time, xi)` might be returned instead, which would be non-sense
    for i in relations:
        dims = []
        for d in i:
            # Only add index if a different Dimension name to avoid dropping conditionals
            # with the same name as the parent
            if d.index.name == d.name:
                dims.append(d)
            else:
                dims.extend([d.index, d])

        implicit_relations.update({tuple(filter_ordered(dims))})

    ordering = Ordering(extra, relations=implicit_relations, mode='partial')

    return ordering


def lower_exprs(expressions, subs=None, **kwargs):
    """
    Lowering an expression consists of the following passes:

        * Indexify functions;
        * Align Indexeds with the computational domain;
        * Apply user-provided substitution;

    Examples
    --------
    f(x - 2*h_x, y) -> f[xi + 2, yi + 4]  (assuming halo_size=4)
    """
    return _lower_exprs(expressions, subs or {})


def _lower_exprs(expressions, subs):
    processed = []
    for expr in as_tuple(expressions):
        try:
            dimension_map = expr.subdomain.dimension_map
        except AttributeError:
            # Some Relationals may be pure SymPy objects, thus lacking the subdomain
            dimension_map = {}

        # Handle Functions (typical case)
        mapper = {f: _lower_exprs(f.indexify(subs=dimension_map), subs)
                  for f in expr.find(AbstractFunction)}

        # Handle Indexeds (from index notation)
        for i in retrieve_indexed(expr):
            f = i.function

            # Introduce shifting to align with the computational domain
            indices = [_lower_exprs(a, subs) + o for a, o in
                       zip(i.indices, f._size_nodomain.left)]

            # Substitute spacing (spacing only used in own dimension)
            indices = [i.xreplace({d.spacing: 1, -d.spacing: -1})
                       for i, d in zip(indices, f.dimensions)]

            # Apply substitutions, if necessary
            if dimension_map:
                indices = [j.xreplace(dimension_map) for j in indices]

            # Handle Array
            if isinstance(f, Array) and f.initvalue is not None:
                initvalue = [_lower_exprs(i, subs) for i in f.initvalue]
                # TODO: fix rebuild to avoid new name
                f = f._rebuild(name='%si' % f.name, initvalue=initvalue)

            mapper[i] = f.indexed[indices]
        # Add dimensions map to the mapper in case dimensions are used
        # as an expression, i.e. Eq(u, x, subdomain=xleft)
        mapper.update(dimension_map)
        # Add the user-supplied substitutions
        mapper.update(subs)
        # Apply mapper to expression
        processed.append(uxreplace(expr, mapper))

    if isinstance(expressions, Iterable):
        return processed
    else:
        assert len(processed) == 1
        return processed.pop()


def concretize_subdims(exprs, **kwargs):
    """
    Given a list of expressions, return a new list where all user-defined
    SubDimensions have been replaced by their concrete counterparts.

    A concrete SubDimension binds objects that are guaranteed to be unique
    across `exprs`, such as the thickness symbols.
    """
    sregistry = kwargs.get('sregistry')

    mapper = {}
    rebuilt = {}  # Rebuilt implicit dims etc which are shared between dimensions

    _concretize_subdims(exprs, mapper, rebuilt, sregistry)
    if not mapper:
        return exprs

    # There may be indexed Arrays defined on SubDimensions in the expressions.
    # These must have their dimensions replaced and their .function attribute
    # reset to prevent recovery of the original SubDimensions.
    functions = set().union(*[set(retrieve_functions(e)) for e in exprs])
    functions = {f for f in functions if f.is_Array}
    for f in functions:
        dimensions = tuple(mapper[d] if d in mapper else d for d in f.dimensions)
        if dimensions != f.dimensions:  # A dimension has been rebuilt
            # So build a mapper for Indexed
            mapper[f.indexed] = f._rebuild(dimensions=dimensions, function=None).indexed

    processed = [uxreplace(e, mapper) for e in exprs]

    return processed


@singledispatch
def _concretize_subdims(a, mapper, rebuilt, sregistry):
    pass


@_concretize_subdims.register(list)
@_concretize_subdims.register(tuple)
def _(v, mapper, rebuilt, sregistry):
    for i in v:
        _concretize_subdims(i, mapper, rebuilt, sregistry)


@_concretize_subdims.register(Eq)
def _(expr, mapper, rebuilt, sregistry):
    for d in expr.free_symbols:
        _concretize_subdims(d, mapper, rebuilt, sregistry)

    # Subdimensions can be hiding in implicit dims
    _concretize_subdims(expr.implicit_dims, mapper, rebuilt, sregistry)


@_concretize_subdims.register(SubDimension)
def _(d, mapper, rebuilt, sregistry):
    if d in mapper:
        # Already have a substitution for this dimension
        return

    tkns = [tkn._rebuild(name=sregistry.make_name(prefix=tkn.name))
            for tkn in d.tkns]
    tkns_subs = {tkn0: tkn1 for tkn0, tkn1 in zip(d.tkns, tkns)}
    left, right = [mM.subs(tkns_subs) for mM in (d.symbolic_min, d.symbolic_max)]
    thickness = tuple((v, d._thickness_map[k]) for k, v in tkns_subs.items())

    mapper[d] = d._rebuild(symbolic_min=left, symbolic_max=right, thickness=thickness)


@_concretize_subdims.register(ConditionalDimension)
def _(d, mapper, rebuilt, sregistry):
    if d in mapper:
        # Already have a substitution for this dimension
        return

    _concretize_subdims(d.parent, mapper, rebuilt, sregistry)

    kwargs = {}

    # Parent may be a subdimension
    if d.parent in mapper:
        kwargs['parent'] = mapper[d.parent]

    # Condition may contain subdimensions
    if d.condition is not None:
        for v in d.condition.free_symbols:
            _concretize_subdims(v, mapper, rebuilt, sregistry)

        if any(v in mapper for v in d.condition.free_symbols):
            # Substitute into condition
            kwargs['condition'] = d.condition.subs(mapper)

    if kwargs:
        # Rebuild if parent or condition need replacing
        mapper[d] = d._rebuild(**kwargs)


@_concretize_subdims.register(MultiSubDimension)
def _(d, mapper, rebuilt, sregistry):
    if d in mapper:
        # Already have a substitution for this dimension
        return

    tkns0 = MultiSubDimension._symbolic_thickness(d.name)
    tkns1 = [tkn._rebuild(name=sregistry.make_name(prefix=tkn.name))
             for tkn in tkns0]

    kwargs = {'thickness': tuple(tkns1), 'functions': d.functions}

    idim0 = d.implicit_dimension
    if idim0 is not None:
        try:
            # Get a preexisiting substitution if one exists
            idim1 = rebuilt[idim0]
            # If a substitution exists for the implicit dimension,
            # then there is also one for the function
            functions = rebuilt[d.functions]
        except KeyError:
            iname = sregistry.make_name(prefix=idim0.name)
            rebuilt[idim0] = idim1 = idim0._rebuild(name=iname)

            fdims = (idim1,) + (d.functions.dimensions[1:])
            frebuilt = d.functions._rebuild(dimensions=fdims, function=None,
                                            halo=None, padding=None,
                                            initializer=d.functions.data)
            rebuilt[d.functions] = functions = frebuilt

        kwargs['implicit_dimension'] = idim1
        kwargs['functions'] = functions

    mapper[d] = d._rebuild(**kwargs)
