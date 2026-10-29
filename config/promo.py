"""Static configuration of the «🎟 Промокоды» section.

The section is deliberately NOT a listing: the project's configured
affiliate/referral page for promo offers is the single destination, and the
bot opens it with a Telegram URL button - no campaign list, no campaign id,
no intermediate screen and no reconstructed link.

Why the URL lives here (and not in an API call):

* the AliExpress affiliate API this app key may call documents campaign
  records (``featuredpromo``), not coupon codes - and showing those internal
  records to a user is explicitly not wanted;
* a URL built at runtime from API data would be a second, uncontrolled
  destination - the requirement is EXACTLY the configured link below.

``PROMO_REFERRAL_URL`` is therefore passed to Telegram verbatim: it is never
modified, never has parameters appended and is never replaced by a plain
marketplace URL.
"""

from __future__ import annotations

#: The exact configured partner/referral page of the «🎟 Промокоды» section.
#: Used verbatim - never rebuilt, never parameterised.
PROMO_REFERRAL_URL = "https://s.click.aliexpress.com/e/_c42dRSbf"
