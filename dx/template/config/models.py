import base64
import uuid
from itertools import chain

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


def id_gen():
    bytes = uuid.uuid4().bytes
    return base64.urlsafe_b64encode(bytes).rstrip(b"=").decode("ascii")


class BaseModel(models.Model):
    id = models.CharField(max_length=255, primary_key=True, default=id_gen, editable=False)
    created = models.DateTimeField(default=timezone.now, blank=True)
    modified = models.DateTimeField(auto_now=True, blank=True)

    class Meta:
        abstract = True
        ordering = ["-created"]

    def to_dict(self):
        # modified from `model_to_dict`
        opts = self._meta
        data = {}
        for f in chain(opts.concrete_fields, opts.private_fields, opts.many_to_many):
            data[f.name] = f.value_from_object(self)
        return data

    def set_payload(self, payload):
        # TODO: add documentation
        for attr, value in payload.dict().items():
            setattr(self, attr, value)

    def set_payload_partial(self, payload):
        # TODO: add documentation
        for attr, value in payload.dict(exclude_unset=True).items():
            setattr(self, attr, value)

    def __str__(self):
        if hasattr(self, "name") and self.name:
            return self.name
        if hasattr(self, "title") and self.title:
            return self.title
        else:
            return f'{self.__class__.__name__}({self.id})'


