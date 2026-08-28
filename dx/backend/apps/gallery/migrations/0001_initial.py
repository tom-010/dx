import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="MediaItem",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                ("file", models.FileField(upload_to="gallery/%Y/%m/")),
                (
                    "kind",
                    models.CharField(
                        choices=[("image", "Image"), ("video", "Video")], max_length=5
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("content_type", models.CharField(max_length=100)),
                ("size", models.PositiveBigIntegerField()),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(class)ss",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created", "-id"], "abstract": False},
        ),
    ]
