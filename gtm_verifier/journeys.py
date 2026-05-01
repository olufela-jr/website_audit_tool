import re
from typing import List

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

import config
from core import (
    CheckResult,
    _click,
    _type,
    accept_consent,
    check_event,
    failed_check,
    get_datalayer_length,
    make_driver,
    skip_check,
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _add_product_to_cart(driver) -> None:
    """Navigate to /shop, click a product card, then click add-to-cart.
    Raises TimeoutException / WebDriverException on selector failure."""
    driver.get(f"{config.BASE_URL}/shop")
    _click(driver, config.PRODUCT_CARD)
    _click(driver, config.ADD_TO_CART_BUTTON)


# ── Journeys ──────────────────────────────────────────────────────────────────

def journey_page_load() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(config.BASE_URL)
        accept_consent(driver)
        idx = 0  # check from the very start — init/page_view fire on load

        results.append(check_event(driver, "init", ["client_id", "user_properties.customer_type"], idx))
        results.append(check_event(driver, "page_view", ["page_title", "page_path"], idx))
    finally:
        driver.quit()
    return results


def journey_consent() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(config.BASE_URL)
        # Capture index *before* accepting so we only catch the new push
        idx = get_datalayer_length(driver)
        accept_consent(driver)
        results.append(check_event(driver, "consent_update", ["consent_choice"], idx))
    finally:
        driver.quit()
    return results


def journey_shop() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(f"{config.BASE_URL}/shop")
        accept_consent(driver)
        idx = 0

        results.append(check_event(driver, "view_item_list", ["ecommerce.items"], idx))

        idx = get_datalayer_length(driver)
        try:
            _click(driver, config.PRODUCT_CARD)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "select_item",
                f"Could not click product card "
                f"(update config.PRODUCT_CARD, currently '{config.PRODUCT_CARD}'): {exc}",
            ))
            return results

        results.append(check_event(driver, "select_item", ["ecommerce.items.0"], idx))
    finally:
        driver.quit()
    return results


def journey_product_detail() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(f"{config.BASE_URL}/shop")
        accept_consent(driver)
        idx = get_datalayer_length(driver)

        try:
            _click(driver, config.PRODUCT_CARD)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "view_item",
                f"Could not click product card "
                f"(update config.PRODUCT_CARD, currently '{config.PRODUCT_CARD}'): {exc}",
            ))
            return results

        results.append(
            check_event(driver, "view_item", ["ecommerce.value", "ecommerce.items.0"], idx)
        )

        idx = get_datalayer_length(driver)
        try:
            _click(driver, config.ADD_TO_CART_BUTTON)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "add_to_cart",
                f"Could not click add-to-cart "
                f"(update config.ADD_TO_CART_BUTTON, currently '{config.ADD_TO_CART_BUTTON}'): {exc}",
            ))
            return results

        results.append(check_event(
            driver, "add_to_cart",
            ["ecommerce.value", "ecommerce.items.0.quantity"],
            idx,
        ))
    finally:
        driver.quit()
    return results


def journey_cart() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        # Seed the cart before navigating to /cart
        driver.get(f"{config.BASE_URL}/shop")
        accept_consent(driver)
        try:
            _click(driver, config.PRODUCT_CARD)
            _click(driver, config.ADD_TO_CART_BUTTON)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "view_cart",
                f"Could not seed cart (check config.PRODUCT_CARD / config.ADD_TO_CART_BUTTON): {exc}",
            ))
            return results

        driver.get(f"{config.BASE_URL}/cart")
        idx = 0
        results.append(
            check_event(driver, "view_cart", ["ecommerce.value", "ecommerce.items"], idx)
        )

        # Remove item
        idx = get_datalayer_length(driver)
        try:
            _click(driver, config.CART_REMOVE_BUTTON)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "remove_from_cart",
                f"Could not click remove button "
                f"(update config.CART_REMOVE_BUTTON, currently '{config.CART_REMOVE_BUTTON}'): {exc}",
            ))
            return results

        results.append(check_event(driver, "remove_from_cart", [], idx))

        # Re-add item, navigate to cart, then proceed to checkout
        try:
            driver.get(f"{config.BASE_URL}/shop")
            _click(driver, config.PRODUCT_CARD)
            _click(driver, config.ADD_TO_CART_BUTTON)
            driver.get(f"{config.BASE_URL}/cart")
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "begin_checkout",
                f"Could not re-add item after removal: {exc}",
            ))
            return results

        idx = get_datalayer_length(driver)
        try:
            _click(driver, config.PROCEED_TO_CHECKOUT_BUTTON)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "begin_checkout",
                f"Could not click proceed-to-checkout "
                f"(update config.PROCEED_TO_CHECKOUT_BUTTON, currently '{config.PROCEED_TO_CHECKOUT_BUTTON}'): {exc}",
            ))
            return results

        results.append(check_event(driver, "begin_checkout", [], idx))
    finally:
        driver.quit()
    return results


def journey_checkout() -> List[CheckResult]:
    # Requires cart state — seeds its own cart before navigating to /checkout
    driver = make_driver()
    results = []
    try:
        driver.get(f"{config.BASE_URL}/shop")
        accept_consent(driver)
        try:
            _click(driver, config.PRODUCT_CARD)
            _click(driver, config.ADD_TO_CART_BUTTON)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "add_shipping_info",
                f"Could not seed cart before checkout journey: {exc}",
            ))
            return results

        driver.get(f"{config.BASE_URL}/checkout")
        idx = get_datalayer_length(driver)

        try:
            _type(driver, config.CHECKOUT_ADDRESS_FIELD, "123 Test Street")
            # Select the first non-placeholder emirate option to trigger shipping tier
            emirate_el = WebDriverWait(driver, config.DEFAULT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, config.CHECKOUT_EMIRATE_FIELD))
            )
            Select(emirate_el).select_by_index(1)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "add_shipping_info",
                f"Could not fill address/emirate fields "
                f"(update config.CHECKOUT_ADDRESS_FIELD / config.CHECKOUT_EMIRATE_FIELD): {exc}",
            ))
            return results

        results.append(check_event(driver, "add_shipping_info", ["ecommerce.shipping_tier"], idx))
    finally:
        driver.quit()
    return results


def journey_purchase() -> List[CheckResult]:
    # TODO: ORDER_CONFIRMATION_URL in config.py must point to a real or stubbed order
    # confirmation page that already has GTM/GA4 loaded and pushes a purchase event.
    driver = make_driver()
    results = []
    try:
        driver.get(config.ORDER_CONFIRMATION_URL)
        accept_consent(driver)
        idx = 0

        purchase_result = check_event(
            driver,
            "purchase",
            [
                "ecommerce.transaction_id",
                "ecommerce.value",
                "ecommerce.shipping",
                "ecommerce.items",
            ],
            idx,
        )
        results.append(purchase_result)

        # Separately validate transaction_id format — reported as its own check
        if purchase_result.event is not None:
            try:
                txn_id = purchase_result.event.get("ecommerce", {}).get("transaction_id", "")
            except AttributeError:
                txn_id = ""
            pattern = r"^PV-\d+$"
            if re.match(pattern, str(txn_id)):
                results.append(CheckResult(
                    name="purchase.transaction_id format",
                    event=purchase_result.event,
                    passed=True,
                    detail=f"transaction_id '{txn_id}' matches {pattern}",
                ))
            else:
                results.append(CheckResult(
                    name="purchase.transaction_id format",
                    event=purchase_result.event,
                    passed=False,
                    detail=f"transaction_id '{txn_id}' does not match {pattern}",
                ))
        else:
            results.append(failed_check(
                "purchase.transaction_id format",
                "Cannot validate — purchase event was not found",
            ))
    finally:
        driver.quit()
    return results


def journey_subscribe() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(f"{config.BASE_URL}/subscribe")
        accept_consent(driver)

        idx = get_datalayer_length(driver)
        try:
            _click(driver, config.PLAN_CARD_SELECTOR)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "select_promotion",
                f"Could not click plan card "
                f"(update config.PLAN_CARD_SELECTOR, currently '{config.PLAN_CARD_SELECTOR}'): {exc}",
            ))
        else:
            results.append(check_event(driver, "select_promotion", [], idx))

        idx = get_datalayer_length(driver)
        try:
            _type(driver, config.SUBSCRIBE_FORM_EMAIL, "test@example.com")
            _click(driver, config.SUBSCRIBE_FORM_SUBMIT)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "generate_lead",
                f"Could not submit subscribe form "
                f"(update config.SUBSCRIBE_FORM_EMAIL / config.SUBSCRIBE_FORM_SUBMIT): {exc}",
            ))
        else:
            results.append(check_event(driver, "generate_lead", [], idx))

        results.append(skip_check("sign_up", "Requires post-verification step"))
    finally:
        driver.quit()
    return results


def journey_contact() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(f"{config.BASE_URL}/contact")
        accept_consent(driver)

        # form_start — triggered by focusing the first field
        idx = get_datalayer_length(driver)
        try:
            el = WebDriverWait(driver, config.DEFAULT_TIMEOUT).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, config.CONTACT_FIRST_FIELD))
            )
            el.click()
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "form_start",
                f"Could not focus first contact form field "
                f"(update config.CONTACT_FIRST_FIELD, currently '{config.CONTACT_FIRST_FIELD}'): {exc}",
            ))
        else:
            results.append(check_event(driver, "form_start", [], idx))

        # form_submit_error — triggered by submitting empty form
        idx = get_datalayer_length(driver)
        try:
            _click(driver, config.CONTACT_SUBMIT_BUTTON)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "form_submit_error",
                f"Could not click contact submit "
                f"(update config.CONTACT_SUBMIT_BUTTON, currently '{config.CONTACT_SUBMIT_BUTTON}'): {exc}",
            ))
        else:
            results.append(
                check_event(driver, "form_submit_error", ["error_fields", "error_count"], idx)
            )

        # form_submit — fill valid data and submit
        idx = get_datalayer_length(driver)
        try:
            _type(driver, config.CONTACT_NAME_FIELD, "Test User")
            _type(driver, config.CONTACT_EMAIL_FIELD, "test@example.com")
            _type(driver, config.CONTACT_MESSAGE_FIELD, "This is an automated verification message.")
            _click(driver, config.CONTACT_SUBMIT_BUTTON)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "form_submit",
                f"Could not fill and submit contact form "
                f"(update config.CONTACT_NAME_FIELD / config.CONTACT_EMAIL_FIELD / "
                f"config.CONTACT_MESSAGE_FIELD / config.CONTACT_SUBMIT_BUTTON): {exc}",
            ))
        else:
            results.append(check_event(driver, "form_submit", [], idx))
    finally:
        driver.quit()
    return results


def journey_search() -> List[CheckResult]:
    driver = make_driver()
    results = []
    try:
        driver.get(config.BASE_URL)
        accept_consent(driver)

        idx = get_datalayer_length(driver)
        try:
            _type(driver, config.SEARCH_INPUT, "test product")
            _click(driver, config.SEARCH_SUBMIT)
        except (TimeoutException, WebDriverException) as exc:
            results.append(failed_check(
                "search",
                f"Could not interact with search "
                f"(update config.SEARCH_INPUT / config.SEARCH_SUBMIT): {exc}",
            ))
        else:
            results.append(
                check_event(driver, "search", ["search_term", "search_result_count"], idx)
            )
    finally:
        driver.quit()
    return results
