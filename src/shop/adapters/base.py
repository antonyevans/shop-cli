"""MerchantAdapter abstract base class.

All v0 adapters (MockAdapter, ShopifyStorefrontAdapter, StripeDemoAdapter)
implement this interface. The CLI layer dispatches to the correct adapter
based on the `adapter` field in merchants.yaml.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from shop.models.commerce import CommerceTXTProduct, SearchFilters


class AdapterError(Exception):
    """Raised when an adapter encounters an unrecoverable error."""


class ProductNotFoundError(AdapterError):
    """Raised when a SKU cannot be found."""


class CheckoutNotSupportedError(AdapterError):
    """Raised when the adapter does not support order creation (→ exit 4)."""


class MerchantAdapter(ABC):
    """Abstract base for all merchant adapters."""

    def __init__(self, slug: str, config: dict) -> None:
        self.slug = slug
        self.config = config

    @abstractmethod
    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        """Search for products matching query.

        Returns normalized CommerceTXT products. Never raises on empty results —
        returns an empty list. Raises AdapterError on network/auth failure.
        """

    @abstractmethod
    async def get_product(self, sku: str) -> CommerceTXTProduct:
        """Return full product detail for a namespaced SKU.

        Raises ProductNotFoundError if not found.
        Raises AdapterError on network/auth failure.
        """

    @abstractmethod
    async def get_capabilities(self) -> dict:
        """Return adapter capabilities dict.

        Example: {"search": True, "order_create": True}
        Used by `shop merchant add` health check.
        """

    @abstractmethod
    async def create_order(
        self,
        sku: str,
        quantity: int,
        mandate_id: str,
        idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        """Submit an order.

        Returns an order result dict on success.
        Raises CheckoutNotSupportedError if adapter cannot process orders (→ exit 4).
        Raises AdapterError on network/auth failure.

        checkout_url: optional Shopify checkout URL passed for storefront routing.
        """
