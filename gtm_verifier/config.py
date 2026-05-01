import os
import yaml

_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

# Module-level constants — set by load(); auto-loaded from config.yaml on import.
BASE_URL = ORDER_CONFIRMATION_URL = None
GTM_ID = GA4_ID = None
DEFAULT_TIMEOUT = EVENT_POLL_TIMEOUT = None
CONSENT_ACCEPT_BUTTON = None
PRODUCT_CARD = None
ADD_TO_CART_BUTTON = None
CART_REMOVE_BUTTON = PROCEED_TO_CHECKOUT_BUTTON = None
CHECKOUT_ADDRESS_FIELD = CHECKOUT_EMIRATE_FIELD = None
PLAN_CARD_SELECTOR = SUBSCRIBE_FORM_EMAIL = SUBSCRIBE_FORM_SUBMIT = None
CONTACT_FIRST_FIELD = CONTACT_NAME_FIELD = CONTACT_EMAIL_FIELD = None
CONTACT_MESSAGE_FIELD = CONTACT_SUBMIT_BUTTON = None
SEARCH_INPUT = SEARCH_SUBMIT = None


def load(path: str = _DEFAULT_CONFIG) -> None:
    """Load (or reload) configuration from a YAML file."""
    global BASE_URL, ORDER_CONFIRMATION_URL
    global GTM_ID, GA4_ID
    global DEFAULT_TIMEOUT, EVENT_POLL_TIMEOUT
    global CONSENT_ACCEPT_BUTTON
    global PRODUCT_CARD
    global ADD_TO_CART_BUTTON
    global CART_REMOVE_BUTTON, PROCEED_TO_CHECKOUT_BUTTON
    global CHECKOUT_ADDRESS_FIELD, CHECKOUT_EMIRATE_FIELD
    global PLAN_CARD_SELECTOR, SUBSCRIBE_FORM_EMAIL, SUBSCRIBE_FORM_SUBMIT
    global CONTACT_FIRST_FIELD, CONTACT_NAME_FIELD, CONTACT_EMAIL_FIELD
    global CONTACT_MESSAGE_FIELD, CONTACT_SUBMIT_BUTTON
    global SEARCH_INPUT, SEARCH_SUBMIT

    with open(path) as f:
        _c = yaml.safe_load(f)

    BASE_URL               = _c["site"]["base_url"]
    ORDER_CONFIRMATION_URL = _c["site"]["order_confirmation_url"]

    GTM_ID = _c["tags"]["gtm_id"]
    GA4_ID = _c["tags"]["ga4_id"]

    DEFAULT_TIMEOUT    = _c["timeouts"]["default"]
    EVENT_POLL_TIMEOUT = _c["timeouts"]["event_poll"]

    _sel = _c["selectors"]
    CONSENT_ACCEPT_BUTTON      = _sel["consent"]["accept_button"]
    PRODUCT_CARD               = _sel["shop"]["product_card"]
    ADD_TO_CART_BUTTON         = _sel["product_detail"]["add_to_cart"]
    CART_REMOVE_BUTTON         = _sel["cart"]["remove_button"]
    PROCEED_TO_CHECKOUT_BUTTON = _sel["cart"]["proceed_to_checkout"]
    CHECKOUT_ADDRESS_FIELD     = _sel["checkout"]["address_field"]
    CHECKOUT_EMIRATE_FIELD     = _sel["checkout"]["emirate_field"]
    PLAN_CARD_SELECTOR         = _sel["subscribe"]["plan_card"]
    SUBSCRIBE_FORM_EMAIL       = _sel["subscribe"]["form_email"]
    SUBSCRIBE_FORM_SUBMIT      = _sel["subscribe"]["form_submit"]
    CONTACT_FIRST_FIELD        = _sel["contact"]["first_field"]
    CONTACT_NAME_FIELD         = _sel["contact"]["name_field"]
    CONTACT_EMAIL_FIELD        = _sel["contact"]["email_field"]
    CONTACT_MESSAGE_FIELD      = _sel["contact"]["message_field"]
    CONTACT_SUBMIT_BUTTON      = _sel["contact"]["submit_button"]
    SEARCH_INPUT               = _sel["search"]["input"]
    SEARCH_SUBMIT              = _sel["search"]["submit"]


# Auto-load default on import
load()
