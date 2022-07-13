from collections import defaultdict
from datetime import date
from enum import Enum
from functools import cached_property, partial
from typing import Any, List, Optional, Type, TypeVar, Union
import forge
from pydantic import create_model
from django.db import models
from fastapi import APIRouter, Security, Path, Body, Depends, Query
from fastapi.security.base import SecurityBase
from ..schemas import Access, Error
from ..utils.fastapi import Pagination, depends_pagination
from ..utils.pydantic_django import transfer_to_orm, TransferAction
from ..utils.fastapi_django import AggregationFunction, aggregation
from ..utils.pydantic import OptionalModel, ReferencedModel, include_reference, to_optional
from ..utils.dict import remove_none
from ..exceptions import ValidationError
from .base import TBaseModel, TCreateModel, TUpdateModel
from . import BaseRouter, RouterSchema, Method


TDjangoModel = TypeVar('TDjangoModel', bound=models.Model)


class DjangoRouterSchema(RouterSchema):
    __router = None

    parent: Optional['DjangoRouterSchema'] = None
    model: Type[TDjangoModel]
    get: Type[TBaseModel]
    create: Type[TCreateModel] = None
    update: Type[TUpdateModel] = None
    delete_status: Optional[Any] = None
    pagination_options: dict = {}
    aggregate_fields: Optional[Type[Enum]] = None
    aggregate_group_by: Optional[Type[Enum]] = None

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)

        if self.create_multi:
            # create_multi is WIP
            raise NotImplementedError("create_multi is not supported for DjangoRouterSchema")

    def _init_list(self):
        self.list = create_model(f"{self.get.__qualname__}List", __module__=self.get.__module__, items=(List[self.get_referenced], ...))

    @property
    def name_singular(self) -> str:
        return self.model.__name__.lower()

    @property
    def related_name_on_parent(self) -> str:
        return self.name

    @property
    def id_field(self):
        name = self.name_singular
        if self.parent:
            name = name.removeprefix(self.parent.name_singular)

        return '%s_id' % name

    @property
    def id_field_placeholder(self):
        return '/{%s}' % self.id_field

    @cached_property
    def model_fields(self):
        def _get_model_fields(model, prefix=''):
            fields = []
            for field in model._meta.get_fields():
                if isinstance(field, (models.ManyToManyRel, models.ManyToOneRel)):
                    fields.append(f'{prefix}{field.name}__count')
                    continue

                if isinstance(field, models.ForeignKey):
                    if self.parent and field.related_model == self.parent.model:
                        continue

                    fields += _get_model_fields(field.related_model, prefix=prefix + field.name + '__')
                    continue

                fields.append(f'{prefix}{field.name}')

            return fields

        return Enum(f'{self.model.__name__}Fields', {field: field for field in _get_model_fields(self.model)})

    @cached_property
    def get_referenced(self):
        if not issubclass(self.get, ReferencedModel):
            return include_reference()(self.get)

        return self.get

    @cached_property
    def update_optional(self):
        if not issubclass(self.update, OptionalModel):
            return to_optional()(self.update)

        return self.update

    @property
    def router(self) -> APIRouter:
        if not self.__router:
            self._create_router()

        return self.__router

    def _create_router(self):
        prefix = f'/{self.name}'
        if self.parent:
            prefix = self.parent.id_field_placeholder + prefix

        self.__router = APIRouter(prefix=prefix)

        if self.list and self.list is not ...:
            self._create_route_list()

        if self.aggregate_fields:
            self._create_route_aggregate()

        if self.create and self.create is not ...:
            self._create_route_post()

        if self.get and self.get is not ...:
            self._create_route_get()

        if self.update and self.update is not ...:
            self._create_route_patch()
            self._create_route_put()

        if self.delete:
            self._create_route_delete()

        child: DjangoRouterSchema
        for child in self.children:
            self.__router.include_router(child.router)

    def _create_route_list(self):
        self.__router.add_api_route('', methods=['GET'], endpoint=self._create_endpoint_list(), response_model=self.list)

    def _create_route_aggregate(self):
        self.__router.add_api_route('/aggregate/{aggregation_function}/{field}', methods=['GET'], endpoint=self._create_endpoint_aggregate())  # TODO response_model

    def _create_route_post(self):
        self.__router.add_api_route('', methods=['POST'], endpoint=self._create_endpoint_post(), response_model=Union[self.get_referenced, List[self.get_referenced]] if self.create_multi else self.get_referenced)

    def _create_route_get(self):
        self.__router.add_api_route(self.id_field_placeholder, methods=['GET'], endpoint=self._create_endpoint_get(), response_model=self.get_referenced)

    def _create_route_patch(self):
        self.__router.add_api_route(self.id_field_placeholder, methods=['PATCH'], endpoint=self._create_endpoint_patch(), response_model=self.get_referenced)

    def _create_route_put(self):
        self.__router.add_api_route(self.id_field_placeholder, methods=['PUT'], endpoint=self._create_endpoint_put(), response_model=self.get_referenced)

    def _create_route_delete(self):
        self.__router.add_api_route(self.id_field_placeholder, methods=['DELETE'], endpoint=self._create_endpoint_delete(), status_code=204)

    def get_queryset(self, parent_ids: Optional[List[str]] = None, access: Optional[Access] = None, is_annotated: bool = False):
        if self.parent:
            return getattr(self.parent.get_queryset(parent_ids[:-1], access, is_annotated).get(pk=parent_ids[-1]), self.related_name_on_parent).filter(self.objects_filter(access))

        queryset = self.model.objects.filter(self.objects_filter(access))
        if is_annotated:
            queryset = queryset.annotate(*[models.Count(field.name) for field in self.model._meta.get_fields() if isinstance(field, (models.ManyToManyRel, models.ManyToOneRel))])

        return queryset

    def objects_filter(self, access: Optional[Access] = None) -> models.Q:
        if hasattr(self.model, 'tenant_id'):
            return models.Q(tenant_id=access.tenant_id)

        return models.Q()

    def objects_get_filtered(
        self,
        *,
        parent_ids: Optional[List[str]] = None,
        access: Optional[Access] = None,
        search: models.Q = models.Q(),
        pagination: Pagination,
    ) -> List[TDjangoModel]:
        return list(pagination.query(self.get_queryset(parent_ids, access).filter(search)))

    def object_get_by_id(self, id: str, parent_ids: Optional[List[str]] = None, access: Optional[Access] = None) -> TDjangoModel:
        return self.get_queryset(parent_ids, access).get(id=id)

    def object_create(self, *, access: Optional[Access] = None, data: Union[TCreateModel, List[TCreateModel]]) -> List[TDjangoModel]:
        if not isinstance(data, list):
            data = [data]

        elif not self.create_multi:
            raise ValidationError(detail=Error(code='create_multi_disabled'))

        instances = []
        for el in data:
            instance: TDjangoModel = self.model()
            if hasattr(self.model, 'tenant_id') and access:
                instance.tenant_id = access.tenant_id

            transfer_to_orm(el, instance, action=TransferAction.CREATE, access=access)
            instances.append(instance)

        return instances

    def object_update(self, *, access: Optional[Access] = None, instance: TDjangoModel, data: TUpdateModel, transfer_action: TransferAction) -> TDjangoModel:
        transfer_to_orm(data, instance, action=transfer_action, exclude_unset=transfer_action == TransferAction.NO_SUBOBJECTS, access=access)

    def object_delete(self, *, access: Optional[Access] = None, instance: TDjangoModel):
        if self.delete_status:
            instance.status = self.delete_status
            instance.save()

        else:
            instance.delete()

    def _get_security_scopes(self, method: Method):
        if self.security_scopes and self.security_scopes.get(method):
            return self.security_scopes.get(method)

        if self.parent:
            return self.parent._get_security_scopes(method)

        return None

    def _security_signature(self, method: Method):
        if not self.security:
            return []

        return [
            forge.kwarg('access', type=Access, default=Security(self.security, scopes=self._get_security_scopes(method))),
        ]

    def _path_signature_id(self, include_self=True):
        ids = self.parent._path_signature_id() if self.parent else []
        if include_self:
            ids.append(forge.kwarg(self.id_field, type=str, default=Path(..., min_length=self.model.id.field.max_length, max_length=self.model.id.field.max_length)))

        return ids

    def _get_ids(self, kwargs: dict, include_self=True):
        ids = self._path_signature_id(include_self=include_self)
        return [kwargs[arg.name] for arg in ids]

    def _get_id(self, kwargs: dict):
        return kwargs[self._path_signature_id()[-1].name]

    def endpoint_list(self, *, access: Optional[Access] = None, pagination: Pagination, search: models.Q = models.Q(), **kwargs):
        ids = self._get_ids(kwargs, include_self=False)
        return self.list(items=[
            self.get_referenced.from_orm(obj)
            for obj in self.objects_get_filtered(
                parent_ids=ids,
                access=access,
                search=search,
                pagination=pagination,
            )
        ])

    def search_filter(self, **kwargs) -> models.Q:
        return models.Q(**remove_none(kwargs))

    def search_filter_fields(self):
        fields = defaultdict(list)
        for field in self.model._meta.get_fields():
            if getattr(field, 'primary_key', False) or field.name == 'tenant_id':
                continue

            field_type = self.model.__annotations__.get(field.name)
            field_name = field.name

            if isinstance(field, models.ForeignKey):
                field_type = str
                field_name += '__id'

                if self.parent and field.related_model == self.parent.model:
                    continue
            
            if isinstance(field, (models.ManyToManyRel, models.ManyToOneRel)):
                continue

            is_exact_search_included = True
            query_options = {
                'default': None,
            }

            if isinstance(field, (
                models.DateField,
                models.DateTimeField,
                models.IntegerField,
                models.DecimalField,
            )):
                variations = [(field_name, field_type)]

                if isinstance(field, models.DateTimeField):
                    variations.append((f'{field_name}__date', date))

                if isinstance(field, (models.DateField, models.DateTimeField)):
                    for variation, type_ in [*variations]:
                        if type_ != date:
                            continue

                        variations.append((f'{variation}__year', int))
                        variations.append((f'{variation}__quarter', int))
                        variations.append((f'{variation}__month', int))
                        variations.append((f'{variation}__day', int))
                        variations.append((f'{variation}__week', int))
                        variations.append((f'{variation}__week_day', int))

                for variation in variations:
                    name = variation
                    type_ = field_type
                    if isinstance(variation, tuple):
                        name, type_ = variation

                    fields[field].append(forge.kwarg(f'{name}__gte', type=Optional[type_], default=Query(**query_options)))
                    fields[field].append(forge.kwarg(f'{name}__lte', type=Optional[type_], default=Query(**query_options)))

            elif isinstance(field, models.CharField):
                if field.choices:
                    fields[field].append(forge.kwarg(f'{field_name}__in', type=Optional[List[field_type]], default=Query(**query_options)))
                    is_exact_search_included = False

                else:
                    query_options['max_length'] = field.max_length
                    fields[field].append(forge.kwarg(f'{field_name}__icontains', type=Optional[field_type], default=Query(**query_options)))

            if is_exact_search_included:
                fields[field].insert(0, forge.kwarg(field_name, type=Optional[field_type], default=Query(**query_options)))

        return [x for xs in fields.values() for x in xs]

    def create_depends_search(self):
        return forge.sign(*self.search_filter_fields())(self.search_filter)

    def _depends_search(self):
        return [
            forge.kwarg('search', type=models.Q, default=Depends(self.create_depends_search())),
            forge.kwarg('pagination', type=Pagination, default=Depends(
                forge.modify('order_by', type=Optional[List[self.model_fields]], default=Query(self.pagination_options.get('default_order_by', list())))(depends_pagination(**self.pagination_options))
            )),
        ]

    def _create_endpoint_list(self):
        return forge.sign(*[
            *self._path_signature_id(include_self=False),
            *self._security_signature(Method.GET_LIST),
            *self._depends_search(),
        ])(self.endpoint_list)

    def endpoint_aggregate(
        self,
        *,
        aggregation_function: AggregationFunction = Path(...),
        field: str = Path(...),
        access: Optional[Access] = None,
        pagination: Pagination,
        group_by: Optional[List[str]] = None,
        search: models.Q = models.Q(),
        **kwargs,
    ):
        ids = self._get_ids(kwargs, include_self=False)

        return aggregation(
            self.get_queryset(ids, access, is_annotated=True),
            q_filters=search,
            aggregation_function=aggregation_function,
            field=field,
            group_by=group_by,
            pagination=pagination,
        )


    def _create_endpoint_aggregate(self):
        return forge.sign(*[arg for arg in [
            *self._path_signature_id(include_self=False),
            *self._security_signature(Method.GET_AGGREGATE),
            forge.kwarg('aggregation_function', type=AggregationFunction, default=Path(...)),
            forge.kwarg('field', type=self.aggregate_fields, default=Path(...)),
            forge.kwarg('group_by', type=Optional[List[self.aggregate_group_by]], default=Query(None)) if self.aggregate_group_by else None,
            *self._depends_search(),
        ] if arg])(self.endpoint_aggregate)

    def endpoint_post(self, *, data: TCreateModel, access: Optional[Access] = None, **kwargs):
        obj = self.object_create(access=access, data=data)
        if len(obj) > 1:
            return [self.get_referenced.from_orm(o) for o in obj]

        return self.get_referenced.from_orm(obj[0])

    def _create_endpoint_post(self):
        create_type = self.create
        if self.create_multi:
            create_type = List[self.create]

        return forge.sign(*[
            *self._path_signature_id(include_self=False),
            forge.kwarg('data', type=create_type, default=Body(...)),
            *self._security_signature(Method.POST),
        ])(self.endpoint_post)

    def _object_get(self, kwargs, access: Optional[Access] = None):
        ids = self._get_ids(kwargs)
        return self.object_get_by_id(ids[-1], parent_ids=ids[:-1], access=access)

    def endpoint_get(self, *, access: Optional[Access] = None, **kwargs):
        obj = self._object_get(kwargs, access=access)
        return self.get_referenced.from_orm(obj)

    def _create_endpoint_get(self):
        return forge.sign(*[
            *self._path_signature_id(),
            *self._security_signature(Method.GET),
        ])(self.endpoint_get)

    def endpoint_patch(self, *, data: TUpdateModel, access: Optional[Access] = None, **kwargs):
        obj = self._object_get(kwargs, access=access)
        self.object_update(access=access, instance=obj, data=data, transfer_action=TransferAction.NO_SUBOBJECTS)
        return self.get_referenced.from_orm(obj)

    def _create_endpoint_patch(self):
        return forge.sign(*[
            *self._path_signature_id(),
            forge.kwarg('data', type=self.update_optional, default=Body(...)), # TODO to_optional()
            *self._security_signature(Method.PATCH),
        ])(self.endpoint_patch)

    def endpoint_put(self, *, data, access: Optional[Access] = None, **kwargs):
        obj = self._object_get(kwargs, access=access)
        self.object_update(access=access, instance=obj, data=data, transfer_action=TransferAction.SYNC)
        return self.get_referenced.from_orm(obj)

    def _create_endpoint_put(self):
        return forge.sign(*[
            *self._path_signature_id(),
            forge.kwarg('data', type=self.update, default=Body(...)),
            *self._security_signature(Method.PUT),
        ])(self.endpoint_put)

    def endpoint_delete(self, *, access: Optional[Access] = None, **kwargs):
        obj = self._object_get(kwargs, access=access)
        self.object_delete(access=access, instance=obj)

        return ''

    def _create_endpoint_delete(self):
        return forge.sign(*[
            *self._path_signature_id(),
            *self._security_signature(Method.DELETE),
        ])(self.endpoint_delete)

    class Config:
        keep_untouched = (cached_property,)
