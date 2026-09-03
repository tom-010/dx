"""What the datasets app tells the user about.

One file per app, by convention: `apps/notifications/apps.py` imports every
`notification_types.py` at startup, which is what registers its types. The dependency points
this way — datasets knows about notifications, notifications knows nothing about datasets.

`describe()` reads the row every time, so re-notifying after a rename refreshes the message in
place rather than adding a second one (`apps/notifications/services.py::notify`).
"""

from apps.datasets.models import Dataset
from apps.notifications.contracts import NotificationData, NotificationType, registry

#: The key `apps/datasets/api.py` passes to `notifications.notify`. It lives here, with the
#: type that answers to it, so a rename is one edit.
DATASET_CREATED = "datasets.created"


@registry.register
class DatasetCreated(NotificationType[Dataset]):
    key = DATASET_CREATED
    model = "datasets.Dataset"
    label = "Dataset created"
    description = "A dataset was added — by hand, or imported from a document."

    def describe(self, obj: Dataset) -> NotificationData:
        return NotificationData(
            title=f"New dataset: {obj.name}"[:200],
            # The dataset's own description, which is optional — so this notification is the
            # worked example of a message that is sometimes a headline and nothing more.
            description=obj.description,
        )
