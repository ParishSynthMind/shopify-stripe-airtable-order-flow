import pytest

from order_flow.signatures import (SignatureError, shopify_signature, stripe_header,
                                   verify_shopify, verify_stripe)

BODY = b'{"id": 1, "total_price": "10.00"}'


def test_shopify_valid():
    verify_shopify("secret", BODY, shopify_signature("secret", BODY))


@pytest.mark.parametrize("header", [None, "", "bm90IGEgcmVhbCBzaWduYXR1cmU="])
def test_shopify_rejects_missing_or_wrong(header):
    with pytest.raises(SignatureError):
        verify_shopify("secret", BODY, header)


def test_shopify_rejects_tampered_body():
    sig = shopify_signature("secret", BODY)
    with pytest.raises(SignatureError):
        verify_shopify("secret", BODY.replace(b"10.00", b"1.00"), sig)


def test_shopify_rejects_when_secret_not_configured():
    with pytest.raises(SignatureError):
        verify_shopify("", BODY, shopify_signature("", BODY))


def test_stripe_valid_within_tolerance():
    header = stripe_header("whsec_x", BODY, timestamp=1_000_000)
    assert verify_stripe("whsec_x", BODY, header, now=1_000_000 + 299) == 1_000_000


def test_stripe_rejects_stale_timestamp():
    header = stripe_header("whsec_x", BODY, timestamp=1_000_000)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_stripe("whsec_x", BODY, header, now=1_000_000 + 301)


def test_stripe_accepts_any_of_several_v1_during_secret_roll():
    good = stripe_header("whsec_new", BODY, timestamp=50).split("v1=")[1]
    header = f"t=50,v1={'0' * 64},v1={good}"
    verify_stripe("whsec_new", BODY, header, now=50)


@pytest.mark.parametrize("header", [None, "garbage", "t=abc,v1=00", "t=50", "v1=00"])
def test_stripe_rejects_malformed(header):
    with pytest.raises(SignatureError):
        verify_stripe("whsec_x", BODY, header, now=50)


def test_stripe_rejects_wrong_secret():
    header = stripe_header("whsec_other", BODY, timestamp=50)
    with pytest.raises(SignatureError, match="does not match"):
        verify_stripe("whsec_x", BODY, header, now=50)
