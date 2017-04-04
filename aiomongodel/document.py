"""Base document class."""
import contextlib
from collections import OrderedDict

import trafaret as t

from bson import ObjectId, SON

from aiomongodel.errors import ValidationError
from aiomongodel.queryset import MotorQuerySet
from aiomongodel.fields import Field, ObjectIdField, SynonymField, _Empty
from aiomongodel.utils import snake_case


class Meta:
    """Storage for Document meta info.

    Attributes:
        collection_name: Name of the document's db collection.
        indexes: List of ``pymongo.IndexModel`` for collection.
        query_class: Query set class to query documents.
        default_query: Each query in query set will be extended using this
            query through ``$and`` operator.
        default_sort: Default sort expression to order documents in ``find``.
        fields: OrderedDict of document fields as ``{field_name => field}``.
        fields_synonyms: Dict of synonyms for field
            as ``{field_name => synonym_name}``.
        codec_options: Collection's codec options.
        read_preference: Collection's read preference.
        write_concern: Collection's write concern.
        read_concern: Collection's read concern.

    """

    OPTIONS = {'query_class', 'collection_name',
               'default_query', 'default_sort',
               'fields', 'fields_synonyms', 'indexes',
               'codec_options', 'read_preference', 'read_concern',
               'write_concern'}

    def __init__(self, **kwargs):
        self._validate_options(kwargs)

        self.query_class = kwargs.get('query_class', None)
        self.collection_name = kwargs.get('collection_name', None)
        self.default_query = kwargs.get('default_query', {})
        self.default_sort = kwargs.get('default_sort', None)
        self.fields = kwargs.get('fields', None)
        self.fields_synonyms = kwargs.get('fields_synonyms', None)
        self._trafaret = None
        self.indexes = kwargs.get('indexes', None)

        self.codec_options = kwargs.get('codec_options', None)
        self.read_preference = kwargs.get('read_preference', None)
        self.read_concern = kwargs.get('read_concern', None)
        self.write_concern = kwargs.get('write_concern', None)

    def _validate_options(self, kwargs):
        keys = set(kwargs.keys())
        diff = keys - self.__class__.OPTIONS
        if diff:
            # TODO: change Exception type
            raise ValueError(
                'Unrecognized Meta options: {0}.'.format(', '.join(diff)))

    def collection(self, db):
        """Get collection for documents.

        Args:
            db: Database object.

        Returns:
            Collection object.
        """
        return db.get_collection(
            self.collection_name,
            read_preference=self.read_preference,
            read_concern=self.read_concern,
            write_concern=self.write_concern,
            codec_options=self.codec_options)

    @property
    def trafaret(self):
        """Return document's trafaret."""
        if self._trafaret is not None:
            return self._trafaret

        doc_trafaret = {}
        for key, item in self.fields.items():
            t_key = t.Key(key, optional=(not item.required))
            doc_trafaret[t_key] = item.trafaret

        self._trafaret = t.Dict(doc_trafaret)
        return self._trafaret


class BaseDocumentMeta(type):
    """Base metaclass for documents."""

    meta_options_class = Meta

    def __new__(mcls, name, bases, namespace):
        """Create new Document class.

        Gather meta options, gather fields, set document's meta.
        """
        new_class = super().__new__(mcls, name, bases, namespace)

        if name in {'Document', 'EmbeddedDocument'}:
            return new_class

        # prepare meta options from Meta class of the new_class if any.
        options = mcls._get_meta_options(new_class)

        # gather fields
        (options['fields'],
         options['fields_synonyms']) = mcls._get_fields(new_class)

        setattr(new_class, 'meta', mcls.meta_options_class(**options))

        return new_class

    @classmethod
    def _get_fields(mcls, new_class):
        """Gather fields and fields' synonyms."""
        # we should search for fields in all bases classes in reverse order
        # than python search for attributes so that fields could be
        # overwritten in subclasses.
        # As bases we use __mro__.
        fields = OrderedDict()
        synonyms = dict()
        # there are no fields in base classes.
        ignore_bases = {'object', 'BaseDocument',
                        'Document', 'EmbeddedDocument'}
        mro_ns_gen = (cls.__dict__
                      for cls in reversed(new_class.__mro__)
                      if cls.__name__ not in ignore_bases)

        for ns in mro_ns_gen:
            for name, item in ns.items():
                if isinstance(item, Field):
                    if not item.name:
                        item.name = name
                    fields[name] = item
                elif isinstance(item, SynonymField):
                    synonyms[item] = name

        return fields, {item.origin_field_name: name
                        for item, name in synonyms.items()}

    @classmethod
    def _get_meta_options(mcls, new_class):
        """Get meta options from Meta class attribute."""
        doc_meta_options = {}
        doc_meta = new_class.__dict__.get('Meta', None)
        if doc_meta:
            doc_meta_options = {key: doc_meta.__dict__[key]
                                for key in doc_meta.__dict__
                                if not key.startswith('__')}

        return doc_meta_options


class DocumentMeta(BaseDocumentMeta):
    """Document metaclass.

    This meta class add ``_id`` field if it is not specified in
    document class.

    Set collection name for document to snake case of the document class name
    if it is not specified in Meta class attribute of a the document class.

    Attributes:
        query_class: Default query set class.
        default_id_field: Field to use as ``_id`` field if it is not
            specified in document class.

    """

    query_class = MotorQuerySet
    default_id_field = ObjectIdField(name='_id', required=True,
                                     default=lambda: ObjectId())

    @classmethod
    def _get_fields(mcls, new_class):
        fields, synonyms = super()._get_fields(new_class)

        # add _id field if needed
        if '_id' not in fields:
            fields['_id'] = mcls.default_id_field
            setattr(new_class, '_id', mcls.default_id_field)
            return fields, synonyms

        if not fields['_id'].required:
            raise ValueError(
                "'{0}._id' field should be required.".format(
                    new_class.__name__))

        return fields, synonyms

    @classmethod
    def _get_meta_options(mcls, new_class):
        meta_options = super()._get_meta_options(new_class)

        if 'collection_name' not in meta_options:
            meta_options['collection_name'] = snake_case(new_class.__name__)

        if 'query_class' not in meta_options:
            meta_options['query_class'] = mcls.query_class

        return meta_options


class EmbeddedDocumentMeta(BaseDocumentMeta):
    """Embedded Document metaclass."""


class BaseDocument(object):
    """Base class for Document and EmbeddedDocument."""

    def __init__(self, *, _empty=False, **kwargs):
        """Initialize document.

        Args:
            _empty (bool): If True return an empty document without setting
                any field.
            **kwargs: Fields values to set. Each key should be a field name
                not a mongo name of the field.

        Raises:
            ValidationError: If there is an error during setting fields
                with values.

        """
        self._data = OrderedDict()
        if _empty:
            return

        meta = self.__class__.meta
        errors = {}
        for field_name, field in meta.fields.items():
            try:
                value = self._get_field_value_from_data(kwargs, field_name)
            except KeyError:
                value = field.default

            if value is _Empty:
                if field.required:
                    errors[field_name] = ValidationError('field is required')
                continue

            try:
                setattr(self, field_name, value)
            except ValidationError as e:
                errors[field_name] = e

        if errors:
            raise ValidationError(error=errors)

    def _get_field_value_from_data(self, data, field_name):
        """Retrieve value from data for given field_name.

        Try use synonym name if field's name is not in data.

        Args:
            data (dict): Data in form {field_name => value}.
            field_name (str): Field's name.

        Raises:
            KeyError: If there is no value in data for given field_name.
        """
        with contextlib.suppress(KeyError):
            return data[field_name]
        # try synonym name
        return data[self.__class__.meta.fields_synonyms[field_name]]

    def _set_son(self, data):
        """Set document's data using mongo data."""
        self._data = OrderedDict()
        for field_name, field in self.meta.fields.items():
            with contextlib.suppress(KeyError):  # ignore missed fields
                self._data[field_name] = field.from_son(data[field.mongo_name])

        return self

    def to_son(self):
        """Convert document to mongo format."""
        son = SON()
        for field_name, field in self.meta.fields.items():
            if field_name in self._data:
                son[field.mongo_name] = field.to_son(self._data[field_name])

        return son

    @classmethod
    def from_son(cls, data):
        """Create document from mongo data.

        This method does not perform data validation and should be used only
        for data loaded from mongo db (we suppose that data stored in db is
        correct).

        Returns:
            Document instance.
        """
        inst = cls(_empty=True)
        inst._set_son(data)
        return inst

    @classmethod
    def from_data(cls, data):
        """Create document from user provided data.

        This method performs data validation.

        Returns:
            Document isinstance.

        Raises:
            ValidationError: If data is not valid.
        """
        try:
            return cls(**data)
        except TypeError:
            raise ValidationError(
                "value can't be converted to {0}".format(cls.__name__))


class Document(BaseDocument, metaclass=DocumentMeta):
    """Base class for documents.

    Each document class should be defined by inheriting from this
    class and specifying fields and optionally meta options using internal
    Meta class.

    Fields are inherited from base classes and can be overwritten.

    Meta options are NOT inherited.

    Possible meta options for ``class Meta``:

    - ``collection_name``: Name of the document's db collection.
    - ``indexes``: List of ``pymongo.IndexModel`` for collection.
    - ``query_class``: Query set class to query documents.
    - ``default_query``: Each query in query set will be extended using
      this query through ``$and`` operator.
    - ``default_sort``: Default sort expression to order documents in
      ``find``.
    - ``codec_options``: Collection's codec options.
    - ``read_preference``: Collection's read preference.
    - ``write_concern``: Collection's write concern.
    - ``read_concern``: Collection's read concern.

    .. note::
        Indexes are not created automatically. Use
        ``MotorQuerySet.create_indexes`` method to create document's indexes.

    Example:

    .. code-block:: python

        from pymongo import IndexModel, ASCENDING, DESCENDING

        class User(Document):
            name = StrField(regexp=r'[a-zA-Z]{6,20}')
            is_active = BoolField(default=True)
            created = DateTimeField(default=lambda: datetime.utcnow())

            class Meta:
                # define a collection name
                collection_name = 'users'
                # define collection indexes. Use
                # await User.q(db).create_indexes()
                # to create them on application startup.
                indexes = [
                    IndexModel([('name', ASCENDING)], unique=True),
                    IndexModel([('created', DESCENDING)])]
                # order by `created` field by default
                default_sort = [('created', DESCENDING)]

        class ActiveUser(User):
            is_active = BoolField(default=True, choices=[True])

            class Meta:
                collection_name = 'users'
                # specify a default query to work ONLY with
                # active users. So for example
                # await ActiveUser.q(db).count({})
                # will count ONLY active users.
                default_query = {'is_active': True}

    """

    @classmethod
    def q(cls, db):
        """Return queryset object."""
        return cls.meta.query_class(cls, db)

    @classmethod
    def coll(cls, db):
        """Return raw collection object."""
        return cls.meta.collection(db)

    @classmethod
    async def create(cls, db, **kwargs):
        """Create document in mongodb.

        Args:
            db: Database instance.
            **kwargs: Document's fields values.

        Returns:
            Created document instance.

        Raises:
            ValidationError: If some fields are not valid.
        """
        inst = cls.from_data(kwargs)
        return await inst.save(db, do_insert=True)

    async def save(self, db, do_insert=False):
        """Save document in mongodb.

        Args:
            db: Database instance.
            do_insert (bool): If ``True`` always perform ``insert_one``, else
                perform ``replace_one`` with ``upsert=True``.
        """
        data = self.to_son()
        if do_insert:
            await self.__class__.q(db).insert_one(data)
        else:
            await self.__class__.q(db).replace_one({'_id': data['_id']},
                                                   data, upsert=True)
        return self

    async def reload(self, db):
        """Reload current object from mongodb."""
        cls = self.__class__
        data = await cls.coll(db).find_one(self.query_id)
        self._set_son(data)
        return self

    async def update(self, db, update_document):
        """Update current object using query.

        Usage:

        .. code-block:: python

            class User(Document):
                name = StrField()
                value = IntField(default=0)

            async def go(db):
                u = await User(name='xxx').save(db)
                await u.update(db,
                               {'$set': {User.name.s: 'yyy'},
                                '$inc': {User.value.s: 1}})

        """
        cls = self.__class__
        count = await cls.q(db).update_one(self.query_id, update_document)
        # TODO: maybe we should return number of updates or raise if it's 0.
        if count > 0:
            await self.reload(db)

        return self

    async def delete(self, db):
        """Delete current object from db."""
        return await self.__class__.q(db).delete_one(self.query_id)

    @property
    def query_id(self):
        return {'_id': self.__class__._id.to_son(self._id)}


class EmbeddedDocument(BaseDocument, metaclass=EmbeddedDocumentMeta):
    """Base class for embedded documents."""
