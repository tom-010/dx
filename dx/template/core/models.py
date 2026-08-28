from django.db import models
from config.models import BaseModel, id_gen
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
import secrets
import string

class User(AbstractUser):
    id = models.CharField(max_length=255, primary_key=True, default=id_gen, editable=False)
    created = models.DateTimeField(default=timezone.now, blank=True)
    modified = models.DateTimeField(auto_now=True, blank=True)

    @staticmethod
    def example(username=None, first_name="John", last_name="Doe"):
        id = id_gen()
        return User(
            username=username or f"{id}@example.com",
            email=f"{id}@example.com",
            first_name=first_name,
            last_name=last_name,
            modified=timezone.now(),
        )

    @property
    def name(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip()



def generate_token():
    """Generate a secure random token"""
    alphabet = string.ascii_letters + string.digits
    return 'tk_' + ''.join(secrets.choice(alphabet) for _ in range(40))

class ApiToken(BaseModel):
    """Fixed API tokens for users (DEBUG mode only)"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_tokens')
    name = models.CharField(max_length=255, help_text="Description of token usage")
    token = models.CharField(max_length=50, unique=True, default=generate_token)
    is_active = models.BooleanField(default=True)
    last_used = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created']

    def __str__(self):
        return f"{self.user.username} - {self.name}"

    def revoke(self):
        """Revoke this token"""
        self.is_active = False
        self.save()

    def touch(self):
        """Update last_used timestamp"""
        self.last_used = timezone.now()
        self.save(update_fields=['last_used'])

class Todo(BaseModel):
    title = models.CharField(max_length=255)
    description = models.TextField()
    done = models.BooleanField(default=False)
    # user = models.ForeignKey(User, on_delete=models.CASCADE)