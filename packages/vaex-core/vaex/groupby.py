import numpy as np
import vaex
import collections
import six

try:
    collections_abc = collections.abc
except AttributeError:
    collections_abc = collections

__all__ = ['GroupBy', 'Grouper', 'BinnerTime']

_USE_DELAY = True

class BinnerBase:
    pass

class BinnerTime(BinnerBase):
    """Bins an expression in a specified datetime resolution.

    Note that different with :class:`Grouper`, all values between the minimum and maximum
    datetime (incremented by the resolution step) will be present in the dataframe.
    A normal groupby would only contain datatime values that are present in the original
    dataframe.

    Example:

    >>> import vaex
    >>> import numpy as np
    >>> t = np.arange('2015-01-01', '2015-02-01', dtype=np.datetime64)
    >>> y = np.arange(len(t))
    >>> df = vaex.from_arrays(t=t, y=y)
    >>> df.groupby(vaex.BinnerTime.per_week(df.t)).agg({'y' : 'sum'})
    #  t                      y
    0  2015-01-01 00:00:00   21
    1  2015-01-08 00:00:00   70
    2  2015-01-15 00:00:00  119
    3  2015-01-22 00:00:00  168
    4  2015-01-29 00:00:00   87

    """
    def __init__(self, expression, resolution='W', df=None, every=1):
        self.resolution = resolution
        self.expression = expression
        self.df = df or expression.ds
        self.sort_indices = None
        # make sure it's an expression
        self.expression = self.df[str(self.expression)]
        self.tmin, self.tmax = self.df[str(self.expression)].minmax()

        self.resolution_type = 'M8[%s]' % self.resolution
        dt = (self.tmax.astype(self.resolution_type) - self.tmin.astype(self.resolution_type))
        self.N = (dt.astype(int).item() + 1)
        # divide by every, and round up
        self.N = (self.N + every - 1) // every
        self.bin_values = np.arange(self.tmin.astype(self.resolution_type), self.tmax.astype(self.resolution_type)+1, every)
        # TODO: we modify the dataframe in place, this is not nice
        self.begin_name = self.df.add_variable('t_begin', self.tmin.astype(self.resolution_type), unique=True)
        # TODO: import integer from future?
        self.binby_expression = str(self.df['%s - %s' % (self.expression.astype(self.resolution_type), self.begin_name)].astype('int') // every)
        self.binner = self.df._binner_ordinal(self.binby_expression, self.N)

    @classmethod
    def per_day(cls, expression, df=None):
        return cls(expression, 'D', df)

    @classmethod
    def per_week(cls, expression, df=None):
        return cls(expression, 'W', df)

    @classmethod
    def per_month(cls, expression, df=None):
        return cls(expression, 'M', df)

    @classmethod
    def per_quarter(cls, expression, df=None, every=1):
        return cls(expression, 'M', df, every=3*every)

    @classmethod
    def per_year(cls, expression, df=None):
        return cls(expression, 'Y', df)


class Grouper(BinnerBase):
    """Bins an expression to a set of unique bins."""
    def __init__(self, expression, df=None, sort=False, pre_sort=True):
        self.df = df or expression.ds
        self.sort = sort
        self.expression = expression
        # make sure it's an expression
        self.expression = self.df[str(self.expression)]
        set = self.df._set(self.expression)
        keys = set.keys()
        if self.sort:
            if pre_sort:
                sort_indices = np.argsort(keys)
                keys = np.array(keys)[sort_indices].tolist()
                set_dict = dict(zip(keys, range(len(keys))))
                set = type(set)(set_dict, set.count, set.nan_count, set.null_count)
                self.sort_indices = None
            else:
                self.sort_indices = np.argsort(keys)
                keys = np.array(keys)[self.sort_indices].tolist()
        else:
            self.sort_indices = None
        self.set = set

        # TODO: we modify the dataframe in place, this is not nice
        basename = 'set_%s' % vaex.utils._python_save_name(str(expression))
        self.setname = self.df.add_variable(basename, self.set, unique=True)

        self.bin_values = keys
        self.binby_expression = '_ordinal_values(%s, %s)' % (self.expression, self.setname)
        self.N = len(self.bin_values)
        if self.set.has_null:
            self.N += 1
            self.bin_values = [None] + self.bin_values
        if self.set.has_nan:
            self.N += 1
            self.bin_values = [np.nan] + self.bin_values
        if self.sort_indices is not None:
            if self.set.has_null and self.set.has_nan:
                self.sort_indices = np.concatenate([[0, 1], self.sort_indices + 2])
            elif self.set.has_null or self.set.has_nan:
                self.sort_indices = np.concatenate([[0], self.sort_indices + 1])
        self.bin_values = self.expression.dtype.create_array(self.bin_values)
        self.binner = self.df._binner_ordinal(self.binby_expression, self.N)


class GrouperCategory(BinnerBase):
    def __init__(self, expression, df=None, sort=False):
        self.df = df or expression.ds
        self.sort = sort
        self.expression = expression
        # make sure it's an expression
        self.expression = self.df[str(self.expression)]

        self.bin_values = self.df.category_labels(self.expression)
        if self.sort:
            # None will always be the first value
            if self.bin_values[0] is None:
                self.sort_indices = np.concatenate([[0], 1 + np.argsort(self.bin_values[1:])])
                self.bin_values = np.array(self.bin_values)[self.sort_indices].tolist()
            else:
                self.sort_indices = np.argsort(self.bin_values)
                self.bin_values = np.array(self.bin_values)[self.sort_indices].tolist()
        else:
            self.sort_indices = None

        self.N = self.df.category_count(self.expression)
        self.min_value = self.df.category_offset(self.expression)
        # TODO: what do we do with null values for categories?
        # if self.set.has_null:
        #     self.N += 1
        #     keys += ['null']
        self.binner = self.df._binner_ordinal(self.expression, self.N, self.min_value)

class GroupByBase(object):
    def __init__(self, df, by, sort=False):
        self.df = df
        self.sort = sort

        if not isinstance(by, collections_abc.Iterable)\
            or isinstance(by, six.string_types):
            by = [by]

        self.by = []
        self.by_original = by
        for by_value in by:
            if not isinstance(by_value, BinnerBase):
                if df.is_category(by_value):
                    by_value = GrouperCategory(df[str(by_value)], sort=sort)
                else:
                    by_value = Grouper(df[str(by_value)], sort=sort)
            self.by.append(by_value)
        # self._waslist, [self.by, ] = vaex.utils.listify(by)
        self.coords1d = [k.bin_values for k in self.by]

        # binby may be an expression based on self.by.expression
        # if we want to have all columns, minus the columns grouped by
        # we should keep track of the original expressions, but binby
        self.groupby_expression = [str(by.expression) for by in self.by]
        self.binners = [by.binner for by in self.by]
        self.grid = vaex.superagg.Grid(self.binners)
        self.shape = [by.N for by in self.by]
        self.dims = self.groupby_expression[:]

    def _agg(self, actions):
        df = self.df
        if isinstance(actions, collections_abc.Mapping):
            actions = list(actions.items())
        elif not isinstance(actions, collections_abc.Iterable)\
            or isinstance(actions, six.string_types):
            actions = [actions]


        grids = {}
        self.counts = None  # if we already do count *, we'd like to know so we don't have to do it ourselves!

        def add(aggregate, column_name=None, override_name=None):
            if column_name is None or override_name is not None:
                column_name = aggregate.pretty_name(override_name)
            aggregate.edges = True  # is this ok to override?
            values = df._agg(aggregate, self.grid, delay=_USE_DELAY)
            grids[column_name] = values
            if isinstance(aggregate, vaex.agg.AggregatorDescriptorBasic)\
                and aggregate.name == 'AggCount'\
                and aggregate.expression == "*"\
                and (aggregate.selection is None or aggregate.selection is False):
                self.counts = values

        for item in actions:
            override_name = None
            if isinstance(item, tuple):
                name, aggregates = item
            else:
                aggregates = item
                name = None

            if not isinstance(aggregates, collections_abc.Iterable)\
                or isinstance(aggregates, six.string_types):
                # not a list, or a string
                aggregates = [aggregates]
            else:
                # if we have a form of {'z': [.., ...]}
                # we want to have a name like z_<agg name>
                if name is not None:
                    override_name = name
            for aggregate in aggregates:
                if isinstance(aggregate, six.string_types) and aggregate == "count":
                    add(vaex.agg.count(), 'count' if name is None else name)
                else:
                    if isinstance(aggregate, six.string_types):
                        aggregate = vaex.agg.aggregates[aggregate]
                    if callable(aggregate):
                        if name is None:
                            # we use all columns
                            for column_name in df.get_column_names():
                                if column_name not in self.groupby_expression:
                                    add(aggregate(column_name), override_name=override_name)
                        else:
                            add(aggregate(name), name, override_name=override_name)
                    else:
                        add(aggregate, name, override_name=override_name)
        return grids

    @property
    def groups(self):
        for group, df in self:
            yield group

    def get_group(self, group):
        values = group
        filter_expressions = [self.df[expression] == value for expression, value in zip(self.groupby_expression, values)]
        filter_expression = filter_expressions[0]
        for expression in filter_expressions[1:]:
            filter_expression = filter_expression & expression
        return self.df[filter_expression]

    def __iter__(self):
        count_agg = vaex.agg.count()
        counts = self.df._agg(count_agg, self.grid)
        mask = counts > 0
        values2d = np.array([coord[mask] for coord in np.meshgrid(*self.coords1d, indexing='ij')], dtype='O')
        for i in range(values2d.shape[1]):
            values = values2d[:,i]
            dff = self.get_group(values)
            yield tuple(values.tolist()), dff

    def __len__(self):
        count_agg = vaex.agg.count()
        counts = self.df._agg(count_agg, self.grid)
        mask = counts > 0
        return mask.sum()


class BinBy(GroupByBase):
    """Implementation of the binning and aggregation of data, see :method:`binby`."""
    def __init__(self, df, by):
        super(BinBy, self).__init__(df, by)

    def agg(self, actions, merge=False):
        import xarray as xr
        arrays = super(BinBy, self)._agg(actions)
        self.df.execute()
        if _USE_DELAY:
            arrays = {key: value.get() for key, value in arrays.items()}
        # take out the edges
        arrays = {key: vaex.utils.extract_central_part(value) for key, value in arrays.items()}

        # make sure we respect the sorting
        sorting = tuple(by.sort_indices if by.sort_indices is not None else slice(None) for by in self.by)
        arrays = {key: value[sorting] for key, value in arrays.items()}

        keys = list(arrays.keys())
        key0 = keys[0]
        if not isinstance(actions, collections_abc.Iterable)\
            or isinstance(actions, six.string_types):
            assert len(keys) == 1
            final_array = arrays[key0]
            coords = self.coords1d
            return xr.DataArray(final_array, coords=coords, dims=self.dims)
        else:
            final_array = np.zeros((len(arrays), ) + arrays[key0].shape)
            for i, value in enumerate(arrays.values()):
                final_array[i] = value
            coords = [list(arrays.keys())] + self.coords1d
            dims = ['statistic'] + self.dims
            return xr.DataArray(final_array, coords=coords, dims=dims)

class GroupBy(GroupByBase):
    """Implementation of the binning and aggregation of data, see :method:`groupby`."""
    def __init__(self, df, by, sort=False):
        super(GroupBy, self).__init__(df, by, sort=sort)

    def agg(self, actions):
        # TODO: this basically forms a cartesian product, we can do better, use a
        # 'multistage' hashmap
        arrays = super(GroupBy, self)._agg(actions)
        # we don't want non-existing pairs (e.g. Amsterdam in France does not exist)
        counts = self.counts
        if counts is None:  # nobody wanted to know count*, but we need it
            count_agg = vaex.agg.count(edges=True)
            counts = self.df._agg(count_agg, self.grid, delay=_USE_DELAY)
        self.df.execute()
        if _USE_DELAY:
            arrays = {key: value.get() for key, value in arrays.items()}
            counts = counts.get()
        # take out the edges
        arrays = {key: vaex.utils.extract_central_part(value) for key, value in arrays.items()}
        counts = vaex.utils.extract_central_part(counts)

        # make sure we respect the sorting
        sorting = tuple(by.sort_indices if by.sort_indices is not None else slice(None) for by in self.by)
        arrays = {key: value[sorting] for key, value in arrays.items()}
        counts = counts[sorting]

        mask = counts > 0
        coords = [coord[mask] for coord in np.meshgrid(*self.coords1d, indexing='ij')]
        columns = {str(by.expression): coord for by, coord in zip(self.by, coords)}
        # df_grouped = vaex.from_dict(labels)
        for key, value in arrays.items():
            columns[key] = value[mask]
        dataset = vaex.dataset.DatasetArrays(columns)
        # df = vaex.d
        dataset = DatasetGroupby(dataset, self.df, self.by_original, actions)
        df_grouped = vaex.from_dataset(dataset)
        return df_grouped


@vaex.dataset.register
class DatasetGroupby(vaex.dataset.DatasetDecorator):
    '''Wraps a resulting dataset from a dataframe groupby, so the groupby can be serialized'''
    snake_name = 'groupby'
    def __init__(self, original, df, by, agg):
        assert isinstance(original, vaex.dataset.DatasetArrays)
        super().__init__(original)
        self.df = df
        self.by = by
        self.agg = agg
        self._row_count = self.original.row_count
        self._create_columns()

    def _create_columns(self):
        # we know original is a DatasetArrays
        self._columns = self.original._columns.copy()
        self._ids = self.original._ids.copy()

    @property
    def _fingerprint(self):
        by = self.by
        by = str(by) if not isinstance(by, (list, tuple)) else list(map(str, by))
        id = vaex.cache.fingerprint(self.original.fingerprint, self.df, by, self.agg)
        return f'dataset-{self.snake_name}-{id}'

    def chunk_iterator(self, *args, **kwargs):
        yield from self.original.chunk_iterator(*args, **kwargs)

    def hashed(self):
        return type(self)(self.original.hashed(), df=self.df, by=self.by, agg=self.agg)

    def _encode(self, encoding):
        by = self.by
        by = str(by) if not isinstance(by, (list, tuple)) else list(map(str, by))
        spec = {
            'dataframe': encoding.encode('dataframe', self.df),
            'by': by,
            'aggregation': encoding.encode_collection('aggregation', self.agg),
        }
        return spec

    @classmethod
    def _decode(cls, encoding, spec):
        df = encoding.decode('dataframe', spec['dataframe'])
        by = spec['by']
        agg = encoding.decode_collection('aggregation', spec['aggregation'])
        dfg = df.groupby(by, agg=agg)
        return DatasetGroupby(dfg.dataset.original, df, by, agg)

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['original']
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.original = self.df.groupby(self.by, agg=self.agg).dataset.original
        # self._create_columns()

    def slice(self, start, end):
        if start == 0 and end == self.row_count:
            return self
        return vaex.dataset.DatasetSlicedArrays(self, start=start, end=end)
