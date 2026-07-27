# services/worker/notifier/mailboxes.py
#
# Mailbox registry — resolves a mailbox NAME (carried in the PDD) to real Gmail
# credentials kept in the environment/secrets. This is what lets each process use
# its own mailbox for sending AND receiving, changeable WITHOUT code:
#
#   PDD:     "mailbox": "invoice"          (safe to store/show — just a name)
#   secrets: MAILBOX_INVOICE_ADDRESS       (email address)
#            MAILBOX_INVOICE_APP_PASSWORD  (app password — SECRET, never in the PDD)
#   optional per-mailbox: MAILBOX_INVOICE_IMAP_HOST / _SMTP_HOST / _SMTP_PORT
#
# If a named mailbox is not fully configured, we fall back to the legacy single
# mailbox (GMAIL_ADDRESS / GMAIL_APP_PASSWORD) so existing deployments keep working.
import os


def _key(name: str) -> str:
    return name.strip().upper().replace("-", "_")


def _default_mailbox():
    addr = os.getenv("GMAIL_ADDRESS")
    pw = os.getenv("GMAIL_APP_PASSWORD")
    if not (addr and pw):
        return None
    return {
        "name": "default",
        "address": addr,
        "app_password": pw,
        "imap_host": os.getenv("IMAP_HOST", "imap.gmail.com"),
        "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587") or "587"),
    }


def get_mailbox(name=None):
    """Resolve a mailbox by name.

    ONE GMAIL = ONE PROCESS. If a process names a mailbox, that mailbox's own
    credentials MUST be configured — we never silently fall back to another
    account, because that would send a process's mail from the wrong address.
    A missing configuration returns None so the caller can log it loudly.
    Only a process with NO mailbox name at all uses the default account."""
    if name:
        k = _key(name)
        addr = os.getenv(f"MAILBOX_{k}_ADDRESS")
        pw = os.getenv(f"MAILBOX_{k}_APP_PASSWORD")
        if not (addr and pw):
            # The "default" name may be served by the legacy GMAIL_* pair.
            if _key(name) == "DEFAULT":
                return _default_mailbox()
            print(f"mailbox '{name}' is not configured: set MAILBOX_{k}_ADDRESS "
                  f"and MAILBOX_{k}_APP_PASSWORD")
            return None
        if addr and pw:
            return {
                "name": name,
                "address": addr,
                "app_password": pw,
                "imap_host": os.getenv(f"MAILBOX_{k}_IMAP_HOST") or os.getenv("IMAP_HOST", "imap.gmail.com"),
                "smtp_host": os.getenv(f"MAILBOX_{k}_SMTP_HOST") or os.getenv("SMTP_HOST", "smtp.gmail.com"),
                "smtp_port": int(os.getenv(f"MAILBOX_{k}_SMTP_PORT") or os.getenv("SMTP_PORT", "587") or "587"),
            }
    return _default_mailbox()


def all_mailboxes():
    """Every configured mailbox (default + all MAILBOX_<NAME>_*), de-duplicated by
    address — used by the email adapter to poll each inbox for replies."""
    boxes = {}
    default = _default_mailbox()
    if default:
        boxes[default["address"]] = default
    for env_key, value in os.environ.items():
        if env_key.startswith("MAILBOX_") and env_key.endswith("_ADDRESS") and value:
            name = env_key[len("MAILBOX_"):-len("_ADDRESS")]
            box = get_mailbox(name)
            if box:
                boxes[box["address"]] = box
    return list(boxes.values())
