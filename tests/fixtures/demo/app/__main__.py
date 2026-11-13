from .services import build
from .util import slugify

service = build()
print(slugify(service.describe(service.create("alice"))))
