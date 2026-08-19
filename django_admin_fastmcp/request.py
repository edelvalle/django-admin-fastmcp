"""admin_request(user, ...) -> HttpRequest (SPEC.md section 6).

An HttpRequest sufficient for ModelAdmin permission and queryset methods.
Carries: user, method (GET for reads, POST for writes), a real admin path
from reverse() so code that inspects request.path resolves sensibly, META
with the host, empty immutable QueryDicts for GET and POST, an unsaved
SessionStore, empty COOKIES, and a message sink.

The message sink matters. Admin actions and save_model overrides routinely
call self.message_user(request, ...), which raises MessageFailure without the
messages framework installed on the request. Collected messages are returned
in the tool result, so an action's own feedback reaches the caller.
"""

from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpRequest, QueryDict
from django.urls import NoReverseMatch, reverse


class MessageSink:
    """Quacks like django.contrib.messages storage, collects into a list."""

    def __init__(self):
        self.collected: list[str] = []

    def add(self, level, message, extra_tags=""):
        self.collected.append(str(message))

    def __iter__(self):
        return iter(self.collected)


def admin_request(user, model_admin=None, method: str = "GET") -> HttpRequest:
    try:
        if model_admin is not None:
            opts = model_admin.model._meta
            path = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
        else:
            path = reverse("admin:index")
    except NoReverseMatch:
        path = "/admin/"

    request = HttpRequest()
    request.method = method
    request.path = request.path_info = path
    request.META = {"SERVER_NAME": "mcp", "SERVER_PORT": "0", "REMOTE_ADDR": "127.0.0.1"}
    request.GET = QueryDict("", mutable=False)
    request.POST = QueryDict("", mutable=False)
    request.COOKIES = {}
    request.user = user
    request.session = SessionStore()
    request._messages = MessageSink()
    return request


def collected_messages(request: HttpRequest) -> list[str]:
    sink = getattr(request, "_messages", None)
    return list(sink) if isinstance(sink, MessageSink) else []
