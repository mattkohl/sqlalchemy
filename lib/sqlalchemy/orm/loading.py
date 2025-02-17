# orm/loading.py
# Copyright (C) 2005-2019 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""private module containing functions used to convert database
rows into object instances and associated state.

the functions here are called primarily by Query, Mapper,
as well as some of the attribute loading strategies.

"""
from __future__ import absolute_import

import collections

from . import attributes
from . import exc as orm_exc
from . import path_registry
from . import strategy_options
from .base import _DEFER_FOR_STATE
from .base import _SET_DEFERRED_EXPIRED
from .util import _none_set
from .util import aliased
from .util import state_str
from .. import exc as sa_exc
from .. import util
from ..sql import util as sql_util


_new_runid = util.counter()


def instances(query, cursor, context):
    """Return an ORM result as an iterator."""

    context.runid = _new_runid()
    context.post_load_paths = {}

    filtered = query._has_mapper_entities

    single_entity = (
        not query._only_return_tuples
        and len(query._entities) == 1
        and query._entities[0].supports_single_entity
    )

    if filtered:
        if single_entity:
            filter_fn = id
        else:

            def filter_fn(row):
                return tuple(
                    id(item) if ent.use_id_for_hash else item
                    for ent, item in zip(query._entities, row)
                )

    try:
        (process, labels) = list(
            zip(
                *[
                    query_entity.row_processor(query, context, cursor)
                    for query_entity in query._entities
                ]
            )
        )

        if not single_entity:
            keyed_tuple = util.lightweight_named_tuple("result", labels)

        while True:
            context.partials = {}

            if query._yield_per:
                fetch = cursor.fetchmany(query._yield_per)
                if not fetch:
                    break
            else:
                fetch = cursor.fetchall()

            if single_entity:
                proc = process[0]
                rows = [proc(row) for row in fetch]
            else:
                rows = [
                    keyed_tuple([proc(row) for proc in process])
                    for row in fetch
                ]

            for path, post_load in context.post_load_paths.items():
                post_load.invoke(context, path)

            if filtered:
                rows = util.unique_list(rows, filter_fn)

            for row in rows:
                yield row

            if not query._yield_per:
                break
    except Exception as err:
        cursor.close()
        util.raise_from_cause(err)


@util.dependencies("sqlalchemy.orm.query")
def merge_result(querylib, query, iterator, load=True):
    """Merge a result into this :class:`.Query` object's Session."""

    session = query.session
    if load:
        # flush current contents if we expect to load data
        session._autoflush()

    autoflush = session.autoflush
    try:
        session.autoflush = False
        single_entity = len(query._entities) == 1
        if single_entity:
            if isinstance(query._entities[0], querylib._MapperEntity):
                result = [
                    session._merge(
                        attributes.instance_state(instance),
                        attributes.instance_dict(instance),
                        load=load,
                        _recursive={},
                        _resolve_conflict_map={},
                    )
                    for instance in iterator
                ]
            else:
                result = list(iterator)
        else:
            mapped_entities = [
                i
                for i, e in enumerate(query._entities)
                if isinstance(e, querylib._MapperEntity)
            ]
            result = []
            keys = [ent._label_name for ent in query._entities]
            keyed_tuple = util.lightweight_named_tuple("result", keys)
            for row in iterator:
                newrow = list(row)
                for i in mapped_entities:
                    if newrow[i] is not None:
                        newrow[i] = session._merge(
                            attributes.instance_state(newrow[i]),
                            attributes.instance_dict(newrow[i]),
                            load=load,
                            _recursive={},
                            _resolve_conflict_map={},
                        )
                result.append(keyed_tuple(newrow))

        return iter(result)
    finally:
        session.autoflush = autoflush


def get_from_identity(session, key, passive):
    """Look up the given key in the given session's identity map,
    check the object for expired state if found.

    """
    instance = session.identity_map.get(key)
    if instance is not None:

        state = attributes.instance_state(instance)

        # expired - ensure it still exists
        if state.expired:
            if not passive & attributes.SQL_OK:
                # TODO: no coverage here
                return attributes.PASSIVE_NO_RESULT
            elif not passive & attributes.RELATED_OBJECT_OK:
                # this mode is used within a flush and the instance's
                # expired state will be checked soon enough, if necessary
                return instance
            try:
                state._load_expired(state, passive)
            except orm_exc.ObjectDeletedError:
                session._remove_newly_deleted([state])
                return None
        return instance
    else:
        return None


def load_on_ident(
    query, key, refresh_state=None, with_for_update=None, only_load_props=None
):
    """Load the given identity key from the database."""

    if key is not None:
        ident = key[1]
        identity_token = key[2]
    else:
        ident = identity_token = None

    return load_on_pk_identity(
        query,
        ident,
        refresh_state=refresh_state,
        with_for_update=with_for_update,
        only_load_props=only_load_props,
        identity_token=identity_token,
    )


def load_on_pk_identity(
    query,
    primary_key_identity,
    refresh_state=None,
    with_for_update=None,
    only_load_props=None,
    identity_token=None,
):

    """Load the given primary key identity from the database."""

    if refresh_state is None:
        q = query._clone()
        q._get_condition()
    else:
        q = query._clone()

    if primary_key_identity is not None:
        mapper = query._mapper_zero()

        (_get_clause, _get_params) = mapper._get_clause

        # None present in ident - turn those comparisons
        # into "IS NULL"
        if None in primary_key_identity:
            nones = set(
                [
                    _get_params[col].key
                    for col, value in zip(
                        mapper.primary_key, primary_key_identity
                    )
                    if value is None
                ]
            )
            _get_clause = sql_util.adapt_criterion_to_null(_get_clause, nones)

        _get_clause = q._adapt_clause(_get_clause, True, False)
        q._criterion = _get_clause

        params = dict(
            [
                (_get_params[primary_key].key, id_val)
                for id_val, primary_key in zip(
                    primary_key_identity, mapper.primary_key
                )
            ]
        )

        q._params = params

    # with_for_update needs to be query.LockmodeArg()
    if with_for_update is not None:
        version_check = True
        q._for_update_arg = with_for_update
    elif query._for_update_arg is not None:
        version_check = True
        q._for_update_arg = query._for_update_arg
    else:
        version_check = False

    if refresh_state and refresh_state.load_options:
        q = q._with_current_path(refresh_state.load_path.parent)
        q = q._conditional_options(refresh_state.load_options)

    q._get_options(
        populate_existing=bool(refresh_state),
        version_check=version_check,
        only_load_props=only_load_props,
        refresh_state=refresh_state,
        identity_token=identity_token,
    )
    q._order_by = None

    try:
        return q.one()
    except orm_exc.NoResultFound:
        return None


def _setup_entity_query(
    context,
    mapper,
    query_entity,
    path,
    adapter,
    column_collection,
    with_polymorphic=None,
    only_load_props=None,
    polymorphic_discriminator=None,
    **kw
):

    if with_polymorphic:
        poly_properties = mapper._iterate_polymorphic_properties(
            with_polymorphic
        )
    else:
        poly_properties = mapper._polymorphic_properties

    quick_populators = {}

    path.set(context.attributes, "memoized_setups", quick_populators)

    for value in poly_properties:
        if only_load_props and value.key not in only_load_props:
            continue
        value.setup(
            context,
            query_entity,
            path,
            adapter,
            only_load_props=only_load_props,
            column_collection=column_collection,
            memoized_populators=quick_populators,
            **kw
        )

    if (
        polymorphic_discriminator is not None
        and polymorphic_discriminator is not mapper.polymorphic_on
    ):

        if adapter:
            pd = adapter.columns[polymorphic_discriminator]
        else:
            pd = polymorphic_discriminator
        column_collection.append(pd)


def _instance_processor(
    mapper,
    context,
    result,
    path,
    adapter,
    only_load_props=None,
    refresh_state=None,
    polymorphic_discriminator=None,
    _polymorphic_from=None,
):
    """Produce a mapper level row processor callable
       which processes rows into mapped instances."""

    # note that this method, most of which exists in a closure
    # called _instance(), resists being broken out, as
    # attempts to do so tend to add significant function
    # call overhead.  _instance() is the most
    # performance-critical section in the whole ORM.

    identity_class = mapper._identity_class

    populators = collections.defaultdict(list)

    props = mapper._prop_set
    if only_load_props is not None:
        props = props.intersection(mapper._props[k] for k in only_load_props)

    quick_populators = path.get(
        context.attributes, "memoized_setups", _none_set
    )

    for prop in props:
        if prop in quick_populators:
            # this is an inlined path just for column-based attributes.
            col = quick_populators[prop]
            if col is _DEFER_FOR_STATE:
                populators["new"].append(
                    (prop.key, prop._deferred_column_loader)
                )
            elif col is _SET_DEFERRED_EXPIRED:
                # note that in this path, we are no longer
                # searching in the result to see if the column might
                # be present in some unexpected way.
                populators["expire"].append((prop.key, False))
            else:
                getter = None
                # the "adapter" can be here via different paths,
                # e.g. via adapter present at setup_query or adapter
                # applied to the query afterwards via eager load subquery.
                # If the column here
                # were already a product of this adapter, sending it through
                # the adapter again can return a totally new expression that
                # won't be recognized in the result, and the ColumnAdapter
                # currently does not accommodate for this.   OTOH, if the
                # column were never applied through this adapter, we may get
                # None back, in which case we still won't get our "getter".
                # so try both against result._getter().  See issue #4048
                if adapter:
                    adapted_col = adapter.columns[col]
                    if adapted_col is not None:
                        getter = result._getter(adapted_col, False)
                if not getter:
                    getter = result._getter(col, False)
                if getter:
                    populators["quick"].append((prop.key, getter))
                else:
                    # fall back to the ColumnProperty itself, which
                    # will iterate through all of its columns
                    # to see if one fits
                    prop.create_row_processor(
                        context, path, mapper, result, adapter, populators
                    )
        else:
            prop.create_row_processor(
                context, path, mapper, result, adapter, populators
            )

    propagate_options = context.propagate_options
    load_path = (
        context.query._current_path + path
        if context.query._current_path.path
        else path
    )

    session_identity_map = context.session.identity_map

    populate_existing = context.populate_existing or mapper.always_refresh
    load_evt = bool(mapper.class_manager.dispatch.load)
    refresh_evt = bool(mapper.class_manager.dispatch.refresh)
    persistent_evt = bool(context.session.dispatch.loaded_as_persistent)
    if persistent_evt:
        loaded_as_persistent = context.session.dispatch.loaded_as_persistent
    instance_state = attributes.instance_state
    instance_dict = attributes.instance_dict
    session_id = context.session.hash_key
    version_check = context.version_check
    runid = context.runid
    identity_token = context.identity_token

    if not refresh_state and _polymorphic_from is not None:
        key = ("loader", path.path)
        if key in context.attributes and context.attributes[key].strategy == (
            ("selectinload_polymorphic", True),
        ):
            selectin_load_via = mapper._should_selectin_load(
                context.attributes[key].local_opts["entities"],
                _polymorphic_from,
            )
        else:
            selectin_load_via = mapper._should_selectin_load(
                None, _polymorphic_from
            )

        if selectin_load_via and selectin_load_via is not _polymorphic_from:
            # only_load_props goes w/ refresh_state only, and in a refresh
            # we are a single row query for the exact entity; polymorphic
            # loading does not apply
            assert only_load_props is None

            callable_ = _load_subclass_via_in(context, path, selectin_load_via)

            PostLoad.callable_for_path(
                context,
                load_path,
                selectin_load_via.mapper,
                selectin_load_via,
                callable_,
                selectin_load_via,
            )

    post_load = PostLoad.for_context(context, load_path, only_load_props)

    if refresh_state:
        refresh_identity_key = refresh_state.key
        if refresh_identity_key is None:
            # super-rare condition; a refresh is being called
            # on a non-instance-key instance; this is meant to only
            # occur within a flush()
            refresh_identity_key = mapper._identity_key_from_state(
                refresh_state
            )
    else:
        refresh_identity_key = None

        pk_cols = mapper.primary_key

        if adapter:
            pk_cols = [adapter.columns[c] for c in pk_cols]
        tuple_getter = result._tuple_getter(pk_cols, True)

    if mapper.allow_partial_pks:
        is_not_primary_key = _none_set.issuperset
    else:
        is_not_primary_key = _none_set.intersection

    def _instance(row):

        # determine the state that we'll be populating
        if refresh_identity_key:
            # fixed state that we're refreshing
            state = refresh_state
            instance = state.obj()
            dict_ = instance_dict(instance)
            isnew = state.runid != runid
            currentload = True
            loaded_instance = False
        else:
            # look at the row, see if that identity is in the
            # session, or we have to create a new one
            identitykey = (identity_class, tuple_getter(row), identity_token)

            instance = session_identity_map.get(identitykey)

            if instance is not None:
                # existing instance
                state = instance_state(instance)
                dict_ = instance_dict(instance)

                isnew = state.runid != runid
                currentload = not isnew
                loaded_instance = False

                if version_check and not currentload:
                    _validate_version_id(mapper, state, dict_, row, adapter)

            else:
                # create a new instance

                # check for non-NULL values in the primary key columns,
                # else no entity is returned for the row
                if is_not_primary_key(identitykey[1]):
                    return None

                isnew = True
                currentload = True
                loaded_instance = True

                instance = mapper.class_manager.new_instance()

                dict_ = instance_dict(instance)
                state = instance_state(instance)
                state.key = identitykey
                state.identity_token = identity_token

                # attach instance to session.
                state.session_id = session_id
                session_identity_map._add_unpresent(state, identitykey)

        # populate.  this looks at whether this state is new
        # for this load or was existing, and whether or not this
        # row is the first row with this identity.
        if currentload or populate_existing:
            # full population routines.  Objects here are either
            # just created, or we are doing a populate_existing

            # be conservative about setting load_path when populate_existing
            # is in effect; want to maintain options from the original
            # load.  see test_expire->test_refresh_maintains_deferred_options
            if isnew and (propagate_options or not populate_existing):
                state.load_options = propagate_options
                state.load_path = load_path

            _populate_full(
                context,
                row,
                state,
                dict_,
                isnew,
                load_path,
                loaded_instance,
                populate_existing,
                populators,
            )

            if isnew:
                if loaded_instance:
                    if load_evt:
                        state.manager.dispatch.load(state, context)
                    if persistent_evt:
                        loaded_as_persistent(context.session, state.obj())
                elif refresh_evt:
                    state.manager.dispatch.refresh(
                        state, context, only_load_props
                    )

                if populate_existing or state.modified:
                    if refresh_state and only_load_props:
                        state._commit(dict_, only_load_props)
                    else:
                        state._commit_all(dict_, session_identity_map)

            if post_load:
                post_load.add_state(state, True)

        else:
            # partial population routines, for objects that were already
            # in the Session, but a row matches them; apply eager loaders
            # on existing objects, etc.
            unloaded = state.unloaded
            isnew = state not in context.partials

            if not isnew or unloaded or populators["eager"]:
                # state is having a partial set of its attributes
                # refreshed.  Populate those attributes,
                # and add to the "context.partials" collection.

                to_load = _populate_partial(
                    context,
                    row,
                    state,
                    dict_,
                    isnew,
                    load_path,
                    unloaded,
                    populators,
                )

                if isnew:
                    if refresh_evt:
                        state.manager.dispatch.refresh(state, context, to_load)

                    state._commit(dict_, to_load)

            if post_load and context.invoke_all_eagers:
                post_load.add_state(state, False)

        return instance

    if mapper.polymorphic_map and not _polymorphic_from and not refresh_state:
        # if we are doing polymorphic, dispatch to a different _instance()
        # method specific to the subclass mapper
        def ensure_no_pk(row):
            identitykey = (
                identity_class,
                tuple([row[column] for column in pk_cols]),
                identity_token,
            )
            if not is_not_primary_key(identitykey[1]):
                raise sa_exc.InvalidRequestError(
                    "Row with identity key %s can't be loaded into an "
                    "object; the polymorphic discriminator column '%s' is "
                    "NULL" % (identitykey, polymorphic_discriminator)
                )

        _instance = _decorate_polymorphic_switch(
            _instance,
            context,
            mapper,
            result,
            path,
            polymorphic_discriminator,
            adapter,
            ensure_no_pk,
        )

    return _instance


def _load_subclass_via_in(context, path, entity):
    mapper = entity.mapper

    zero_idx = len(mapper.base_mapper.primary_key) == 1

    if entity.is_aliased_class:
        q, enable_opt, disable_opt = mapper._subclass_load_via_in(entity)
    else:
        q, enable_opt, disable_opt = mapper._subclass_load_via_in_mapper

    def do_load(context, path, states, load_only, effective_entity):
        orig_query = context.query

        q2 = q._with_lazyload_options(
            (enable_opt,) + orig_query._with_options + (disable_opt,),
            path.parent,
            cache_path=path,
        )

        if orig_query._populate_existing:
            q2.add_criteria(lambda q: q.populate_existing())

        q2(context.session).params(
            primary_keys=[
                state.key[1][0] if zero_idx else state.key[1]
                for state, load_attrs in states
            ]
        ).all()

    return do_load


def _populate_full(
    context,
    row,
    state,
    dict_,
    isnew,
    load_path,
    loaded_instance,
    populate_existing,
    populators,
):
    if isnew:
        # first time we are seeing a row with this identity.
        state.runid = context.runid

        for key, getter in populators["quick"]:
            dict_[key] = getter(row)
        if populate_existing:
            for key, set_callable in populators["expire"]:
                dict_.pop(key, None)
                if set_callable:
                    state.expired_attributes.add(key)
        else:
            for key, set_callable in populators["expire"]:
                if set_callable:
                    state.expired_attributes.add(key)
        for key, populator in populators["new"]:
            populator(state, dict_, row)
        for key, populator in populators["delayed"]:
            populator(state, dict_, row)
    elif load_path != state.load_path:
        # new load path, e.g. object is present in more than one
        # column position in a series of rows
        state.load_path = load_path

        # if we have data, and the data isn't in the dict, OK, let's put
        # it in.
        for key, getter in populators["quick"]:
            if key not in dict_:
                dict_[key] = getter(row)

        # otherwise treat like an "already seen" row
        for key, populator in populators["existing"]:
            populator(state, dict_, row)
            # TODO:  allow "existing" populator to know this is
            # a new path for the state:
            # populator(state, dict_, row, new_path=True)

    else:
        # have already seen rows with this identity in this same path.
        for key, populator in populators["existing"]:
            populator(state, dict_, row)

            # TODO: same path
            # populator(state, dict_, row, new_path=False)


def _populate_partial(
    context, row, state, dict_, isnew, load_path, unloaded, populators
):

    if not isnew:
        to_load = context.partials[state]
        for key, populator in populators["existing"]:
            if key in to_load:
                populator(state, dict_, row)
    else:
        to_load = unloaded
        context.partials[state] = to_load

        for key, getter in populators["quick"]:
            if key in to_load:
                dict_[key] = getter(row)
        for key, set_callable in populators["expire"]:
            if key in to_load:
                dict_.pop(key, None)
                if set_callable:
                    state.expired_attributes.add(key)
        for key, populator in populators["new"]:
            if key in to_load:
                populator(state, dict_, row)
        for key, populator in populators["delayed"]:
            if key in to_load:
                populator(state, dict_, row)
    for key, populator in populators["eager"]:
        if key not in unloaded:
            populator(state, dict_, row)

    return to_load


def _validate_version_id(mapper, state, dict_, row, adapter):

    version_id_col = mapper.version_id_col

    if version_id_col is None:
        return

    if adapter:
        version_id_col = adapter.columns[version_id_col]

    if (
        mapper._get_state_attr_by_column(state, dict_, mapper.version_id_col)
        != row[version_id_col]
    ):
        raise orm_exc.StaleDataError(
            "Instance '%s' has version id '%s' which "
            "does not match database-loaded version id '%s'."
            % (
                state_str(state),
                mapper._get_state_attr_by_column(
                    state, dict_, mapper.version_id_col
                ),
                row[version_id_col],
            )
        )


def _decorate_polymorphic_switch(
    instance_fn,
    context,
    mapper,
    result,
    path,
    polymorphic_discriminator,
    adapter,
    ensure_no_pk,
):
    if polymorphic_discriminator is not None:
        polymorphic_on = polymorphic_discriminator
    else:
        polymorphic_on = mapper.polymorphic_on
    if polymorphic_on is None:
        return instance_fn

    if adapter:
        polymorphic_on = adapter.columns[polymorphic_on]

    def configure_subclass_mapper(discriminator):
        try:
            sub_mapper = mapper.polymorphic_map[discriminator]
        except KeyError:
            raise AssertionError(
                "No such polymorphic_identity %r is defined" % discriminator
            )
        else:
            if sub_mapper is mapper:
                return None

            return _instance_processor(
                sub_mapper,
                context,
                result,
                path,
                adapter,
                _polymorphic_from=mapper,
            )

    polymorphic_instances = util.PopulateDict(configure_subclass_mapper)

    getter = result._getter(polymorphic_on)

    def polymorphic_instance(row):
        discriminator = getter(row)
        if discriminator is not None:
            _instance = polymorphic_instances[discriminator]
            if _instance:
                return _instance(row)
            else:
                return instance_fn(row)
        else:
            ensure_no_pk(row)
            return None

    return polymorphic_instance


class PostLoad(object):
    """Track loaders and states for "post load" operations.

    """

    __slots__ = "loaders", "states", "load_keys"

    def __init__(self):
        self.loaders = {}
        self.states = util.OrderedDict()
        self.load_keys = None

    def add_state(self, state, overwrite):
        # the states for a polymorphic load here are all shared
        # within a single PostLoad object among multiple subtypes.
        # Filtering of callables on a per-subclass basis needs to be done at
        # the invocation level
        self.states[state] = overwrite

    def invoke(self, context, path):
        if not self.states:
            return
        path = path_registry.PathRegistry.coerce(path)
        for token, limit_to_mapper, loader, arg, kw in self.loaders.values():
            states = [
                (state, overwrite)
                for state, overwrite in self.states.items()
                if state.manager.mapper.isa(limit_to_mapper)
            ]
            if states:
                loader(context, path, states, self.load_keys, *arg, **kw)
        self.states.clear()

    @classmethod
    def for_context(cls, context, path, only_load_props):
        pl = context.post_load_paths.get(path.path)
        if pl is not None and only_load_props:
            pl.load_keys = only_load_props
        return pl

    @classmethod
    def path_exists(self, context, path, key):
        return (
            path.path in context.post_load_paths
            and key in context.post_load_paths[path.path].loaders
        )

    @classmethod
    def callable_for_path(
        cls, context, path, limit_to_mapper, token, loader_callable, *arg, **kw
    ):
        if path.path in context.post_load_paths:
            pl = context.post_load_paths[path.path]
        else:
            pl = context.post_load_paths[path.path] = PostLoad()
        pl.loaders[token] = (token, limit_to_mapper, loader_callable, arg, kw)


def load_scalar_attributes(mapper, state, attribute_names):
    """initiate a column-based attribute refresh operation."""

    # assert mapper is _state_mapper(state)
    session = state.session
    if not session:
        raise orm_exc.DetachedInstanceError(
            "Instance %s is not bound to a Session; "
            "attribute refresh operation cannot proceed" % (state_str(state))
        )

    has_key = bool(state.key)

    result = False

    # in the case of inheritance, particularly concrete and abstract
    # concrete inheritance, the class manager might have some keys
    # of attributes on the superclass that we didn't actually map.
    # These could be mapped as "concrete, dont load" or could be completely
    # exluded from the mapping and we know nothing about them.  Filter them
    # here to prevent them from coming through.
    if attribute_names:
        attribute_names = attribute_names.intersection(mapper.attrs.keys())

    if mapper.inherits and not mapper.concrete:
        # because we are using Core to produce a select() that we
        # pass to the Query, we aren't calling setup() for mapped
        # attributes; in 1.0 this means deferred attrs won't get loaded
        # by default
        statement = mapper._optimized_get_statement(state, attribute_names)
        if statement is not None:
            wp = aliased(mapper, statement)
            result = load_on_ident(
                session.query(wp)
                .options(strategy_options.Load(wp).undefer("*"))
                .from_statement(statement),
                None,
                only_load_props=attribute_names,
                refresh_state=state,
            )

    if result is False:
        if has_key:
            identity_key = state.key
        else:
            # this codepath is rare - only valid when inside a flush, and the
            # object is becoming persistent but hasn't yet been assigned
            # an identity_key.
            # check here to ensure we have the attrs we need.
            pk_attrs = [
                mapper._columntoproperty[col].key for col in mapper.primary_key
            ]
            if state.expired_attributes.intersection(pk_attrs):
                raise sa_exc.InvalidRequestError(
                    "Instance %s cannot be refreshed - it's not "
                    " persistent and does not "
                    "contain a full primary key." % state_str(state)
                )
            identity_key = mapper._identity_key_from_state(state)

        if (
            _none_set.issubset(identity_key) and not mapper.allow_partial_pks
        ) or _none_set.issuperset(identity_key):
            util.warn_limited(
                "Instance %s to be refreshed doesn't "
                "contain a full primary key - can't be refreshed "
                "(and shouldn't be expired, either).",
                state_str(state),
            )
            return

        result = load_on_ident(
            session.query(mapper),
            identity_key,
            refresh_state=state,
            only_load_props=attribute_names,
        )

    # if instance is pending, a refresh operation
    # may not complete (even if PK attributes are assigned)
    if has_key and result is None:
        raise orm_exc.ObjectDeletedError(state)
