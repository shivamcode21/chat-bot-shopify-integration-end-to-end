"""
Vendor Configuration Manager
Defines the dependency graph and enrichment pipeline for each client/vendor combination.
"""
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

class VendorType(Enum):
    """Supported vendor types"""
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    MAGENTO = "magento"
    CUSTOM = "custom"

class LogisticsProvider(Enum):
    """Supported logistics providers"""
    SHIPROCKET = "shiprocket"
    DELHIVERY = "delhivery"
    NONE = "none"


# ============================================
# DELIVERY PARTNER ALIAS MAPPING
# ============================================
# Maps tracking_company names (as returned in Shopify fulfillments[].tracking_company)
# to their canonical LogisticsProvider enum value.
# This is used to determine if the delivery partner on an order is integrated.
DELIVERY_PARTNER_ALIASES: Dict[str, str] = {
    # Shiprocket and its courier partners (tracking_company values Shiprocket sets in Shopify)
    "shiprocket": "shiprocket",
    "shiprocket assigned": "shiprocket",
    "bluedart": "shiprocket",
    "blue dart": "shiprocket",
    "delhivery": "delhivery",
    "ecom express": "shiprocket",
    "ekart": "shiprocket",
    "xpressbees": "shiprocket",
    "shadowfax": "shiprocket",
    "dtdc": "shiprocket",
    "india post": "shiprocket",
    "amazon shipping": "shiprocket",
    "movin": "shiprocket",
    "valmo": "shiprocket",
    "professional couriers": "shiprocket",
    # ClickPost is an aggregator, not a courier, so it never appears as a
    # tracking_company -- Shopify records the carrier underneath it. It is listed
    # here because resolve_partner_from_url hands its own name to
    # ais_integrated_delivery_partner, which translates every name through this
    # table and reports "not integrated" for anything it cannot resolve. Without
    # an entry a tenant with ClickPost genuinely connected reads as un-integrated.
    "clickpost": "clickpost",
}

# Pre-sorted by descending length so longer, more specific aliases match
# before shorter prefixes (e.g. "blue dart" before "blue").
_SORTED_ALIAS_KEYS: List[str] = sorted(
    DELIVERY_PARTNER_ALIASES.keys(), key=len, reverse=True,
)


def resolve_partner_alias(tracking_company: Optional[str]) -> Optional[str]:
    """Resolve a Shopify tracking_company string to its canonical partner name.

    Tries exact match first (O(1)), then falls back to prefix matching so
    variants like "BlueDart Surface 2KG" or "Ecom Express Heavy" resolve
    correctly without enumerating every Shiprocket courier suffix.
    """
    if not tracking_company:
        return None
    normalized = tracking_company.strip().lower()
    if not normalized:
        return None

    exact = DELIVERY_PARTNER_ALIASES.get(normalized)
    if exact:
        return exact

    for alias_key in _SORTED_ALIAS_KEYS:
        if normalized.startswith(alias_key):
            return DELIVERY_PARTNER_ALIASES[alias_key]

    return None


# Ordered list of (url_substring, canonical_partner) pairs.
# Shiprocket can dispatch orders via Delhivery as the underlying courier,
# making tracking_company="Delhivery" even when the order is actually managed
# through Shiprocket's platform. The tracking_url domain is the authoritative
# signal for which platform *manages* the shipment.
# Checked in order — first match wins. Add new partners here as integrations grow.
# ClickPost is listed first because it can dispatch via Shiprocket as the
# underlying courier (tenant URLs carry a cp_id per courier, including one for
# Shiprocket), so a ClickPost-managed URL must not be claimed by a later
# aggregator pattern.
TRACKING_URL_PATTERNS: List[Tuple[str, str]] = [
    ("clickpost", "clickpost"),
    ("delhivery", "delhivery"),
    ("shiprocket", "shiprocket"),
]


def resolve_partner_from_url(tracking_url: Optional[str]) -> Optional[str]:
    """Resolve the canonical logistics partner from a tracking URL substring.

    This is the *primary* resolution signal, preferred over tracking_company,
    because Shiprocket can assign Delhivery as the underlying courier and write
    tracking_company='Delhivery' into Shopify — even though all API interactions
    (track, update, cancel) go through Shiprocket. The tracking_url domain
    unambiguously identifies which platform manages the shipment.

    Examples:
        "https://www.delhivery.com/track/package/123"  → "delhivery"
        "https://shiprocket.co/tracking/ABC123"         → "shiprocket"
        "https://track.bluedart.com/..."                → None (fallback to alias)

    Returns:
        Canonical partner name (lowercase) or None if no pattern matches.
    """
    if not tracking_url:
        return None
    lower_url = tracking_url.strip().lower()
    for pattern, canonical in TRACKING_URL_PATTERNS:
        if pattern in lower_url:
            return canonical
    return None

@dataclass
class EnrichmentStep:
    """Defines a single enrichment step in the pipeline"""
    enricher_type: str  # e.g., "graphql", "tracking", "shiprocket_status"
    enabled: bool = True
    config: Dict[str, Any] = None  # Additional config for this enricher
    
    def __post_init__(self):
        if self.config is None:
            self.config = {}

@dataclass
class VendorConfig:
    """
    Complete vendor configuration defining:
    - Primary order source
    - Logistics provider
    - Enrichment pipeline (dependency graph)
    """
    client_id: str
    primary_vendor: VendorType
    logistics_provider: LogisticsProvider
    
    # Enrichment Pipeline (Dependency Graph)
    # These run in ORDER after fetching from primary vendor
    enrichment_pipeline: List[EnrichmentStep]
    
    # Additional vendor-specific settings
    fetch_from_multiple_sources: bool = False  # If True, fetch from both order + logistics
    fallback_vendor: Optional[VendorType] = None
    
    # Order Status specific config
    order_status_primary_source: str = "logistics"  # "logistics" or "order_vendor"
    
    # Exchange Orders
    exchange_order_suffix: Optional[str] = "-EXC"  # Suffix to append for exchange lookup. None = disable.


# ============================================
# CLIENT CONFIGURATIONS (Dependency Graphs)
# ============================================

# Default Shopify Configuration
# NOTE: Logistics providers are now resolved from DB via AsyncVendorConfigManager.
# The logistics_provider field here is only used by the legacy sync VendorConfigManager methods.
DEFAULT_SHOPIFY_CONFIG = VendorConfig(
    client_id="default",
    primary_vendor=VendorType.SHOPIFY,
    logistics_provider=LogisticsProvider.SHIPROCKET,
    enrichment_pipeline=[
        EnrichmentStep(
            enricher_type="shopify_graphql",
            enabled=False,
            config={"top_n_orders": 5}
        ),
        EnrichmentStep(
            enricher_type="shopify_tracking",
            enabled=False,
            config={"only_if_missing": True}
        ),
        EnrichmentStep(
            enricher_type="shiprocket_status",
            enabled=False,
            config={"top_n_orders": 3}
        ),
    ],
    fetch_from_multiple_sources=False,
    order_status_primary_source="logistics"
)


def build_unconfigured_vendor_config(client_id: Optional[str] = None) -> VendorConfig:
    """Return a safe non-integrated config instead of defaulting to another tenant."""
    return VendorConfig(
        client_id=client_id or "unconfigured",
        primary_vendor=VendorType.SHOPIFY,
        logistics_provider=LogisticsProvider.NONE,
        enrichment_pipeline=[],
        fetch_from_multiple_sources=False,
        fallback_vendor=None,
        order_status_primary_source="order_vendor",
        exchange_order_suffix=None,
    )


# ============================================
# CONFIGURATION REGISTRY
# ============================================

# Client ID -> Order vendor configuration.
# Logistics providers are resolved at runtime from the delivery_partner_integrations
# DB table via AsyncVendorConfigManager (see below).
CLIENT_VENDOR_CONFIGS: Dict[str, VendorConfig] = {
    "default": DEFAULT_SHOPIFY_CONFIG,
}


class VendorConfigManager:
    """
    Central manager for vendor configurations.
    This is the SINGLE SOURCE OF TRUTH for dependency graphs.
    """
    
    @staticmethod
    def get_config(client_id: Optional[str] = None, state: Optional[Dict] = None) -> VendorConfig:
        """
        Get vendor configuration for a client.
        Priority: explicit client_id > state['client_id'].
        No implicit default tenant fallback is allowed in multitenant runtime flows.
        """
        if not client_id and state:
            client_id = state.get('client_id')

        if not client_id:
            return build_unconfigured_vendor_config()

        return CLIENT_VENDOR_CONFIGS.get(client_id) or build_unconfigured_vendor_config(client_id)
    
    @staticmethod
    def get_exchange_suffix(client_id: Optional[str] = None, state: Optional[Dict] = None) -> Optional[str]:
        """Get the exchange order suffix for the client configuration."""
        config = VendorConfigManager.get_config(client_id, state)
        return config.exchange_order_suffix

    @staticmethod
    def get_primary_vendor(client_id: Optional[str] = None, state: Optional[Dict] = None) -> str:
        """Get primary vendor string for a client"""
        config = VendorConfigManager.get_config(client_id, state)
        return config.primary_vendor.value
    
    @staticmethod
    def get_logistics_provider(client_id: Optional[str] = None, state: Optional[Dict] = None) -> str:
        """Get logistics provider string for a client"""
        config = VendorConfigManager.get_config(client_id, state)
        return config.logistics_provider.value
    
    @staticmethod
    def get_enrichment_pipeline_config(client_id: Optional[str] = None, state: Optional[Dict] = None) -> List[EnrichmentStep]:
        """Get enrichment pipeline configuration for a client"""
        config = VendorConfigManager.get_config(client_id, state)
        return [step for step in config.enrichment_pipeline if step.enabled]
    
    @staticmethod
    def should_fetch_from_multiple_sources(client_id: Optional[str] = None, state: Optional[Dict] = None) -> bool:
        """Check if we should fetch from both order vendor and logistics"""
        config = VendorConfigManager.get_config(client_id, state)
        return config.fetch_from_multiple_sources
    
    @staticmethod
    def get_order_status_primary_source(client_id: Optional[str] = None, state: Optional[Dict] = None) -> str:
        """Get primary source for order status lookup"""
        config = VendorConfigManager.get_config(client_id, state)
        return config.order_status_primary_source
    
    @staticmethod
    def has_logistics_provider(client_id: Optional[str] = None, state: Optional[Dict] = None) -> bool:
        """Check if client has a logistics provider configured"""
        config = VendorConfigManager.get_config(client_id, state)
        return config.logistics_provider != LogisticsProvider.NONE

    @staticmethod
    def is_integrated_delivery_partner(tracking_company: str, client_id: Optional[str] = None, state: Optional[Dict] = None) -> bool:
        """
        Check if a tracking_company (from Shopify fulfillments) maps to one of the 
        client's integrated logistics providers.
        
        Args:
            tracking_company: The tracking_company value from Shopify fulfillments[].tracking_company
            client_id: Client ID
            state: State dictionary
            
        Returns:
            True if the delivery partner is integrated for this client, False otherwise
        """
        if not tracking_company:
            return False
        
        config = VendorConfigManager.get_config(client_id, state)
        
        # If client has no logistics provider, nothing is integrated
        if config.logistics_provider == LogisticsProvider.NONE:
            return False
        
        canonical = resolve_partner_alias(tracking_company)
        if not canonical:
            return False
        
        # Check if the canonical provider matches the client's configured logistics provider
        return canonical == config.logistics_provider.value

    @staticmethod
    def get_integrated_providers_list(client_id: Optional[str] = None, state: Optional[Dict] = None) -> List[str]:
        """
        Get list of integrated logistics provider names for a client.
        
        Returns:
            List of provider name strings (e.g., ["shiprocket"])
        """
        config = VendorConfigManager.get_config(client_id, state)
        if config.logistics_provider == LogisticsProvider.NONE:
            return []
        return [config.logistics_provider.value]


# ============================================
# ASYNC DB-BACKED LOGISTICS METHODS
# ============================================

class AsyncVendorConfigManager:
    """
    Async logistics configuration backed by the delivery_partner_integrations DB table.
    Order vendor (Shopify) stays hardcoded in VendorConfigManager; logistics comes from DB.
    """

    @staticmethod
    def _extract_client_id(client_id: Optional[str] = None, state: Optional[Dict] = None) -> Optional[str]:
        if not client_id and state:
            client_id = state.get("client_id")
        return client_id

    @staticmethod
    async def _aget_connected_partners(client_id: Optional[str] = None, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Fetch connected delivery partners from DB with caching.
        Falls back to the hardcoded VendorConfig.logistics_provider if the DB
        returns no results (e.g., DB unreachable, missing rows)."""
        from fashion_bot.utils.delivery_utils import aget_all_delivery_partners_cached
        cid = AsyncVendorConfigManager._extract_client_id(client_id, state)
        if not cid:
            return []
        partners = await aget_all_delivery_partners_cached(cid)
        connected = [p for p in partners if p.get("connected")]
        if connected:
            return connected
        config = VendorConfigManager.get_config(cid, state)
        if config.logistics_provider != LogisticsProvider.NONE:
            return [{"name": config.logistics_provider.value, "connected": True}]
        return []

    @staticmethod
    async def aget_integrated_providers_list(client_id: Optional[str] = None, state: Optional[Dict] = None) -> List[str]:
        """Get list of connected logistics provider names from DB."""
        partners = await AsyncVendorConfigManager._aget_connected_partners(client_id, state)
        return [p["name"].lower() for p in partners]

    @staticmethod
    async def ais_integrated_delivery_partner(
        tracking_company: str, client_id: Optional[str] = None, state: Optional[Dict] = None,
    ) -> bool:
        """Check if tracking_company maps to a connected logistics provider in DB."""
        if not tracking_company:
            return False
        canonical = resolve_partner_alias(tracking_company)
        if not canonical:
            return False
        providers = await AsyncVendorConfigManager.aget_integrated_providers_list(client_id, state)
        return canonical in providers

    @staticmethod
    async def ahas_logistics_provider(client_id: Optional[str] = None, state: Optional[Dict] = None) -> bool:
        """Check if client has any connected logistics provider in DB."""
        providers = await AsyncVendorConfigManager.aget_integrated_providers_list(client_id, state)
        return len(providers) > 0

    @staticmethod
    async def aget_logistics_provider(client_id: Optional[str] = None, state: Optional[Dict] = None) -> str:
        """Get the primary (first) connected logistics provider name from DB.
        Returns 'none' if no provider is connected."""
        providers = await AsyncVendorConfigManager.aget_integrated_providers_list(client_id, state)
        return providers[0] if providers else "none"


# ============================================
# HELPER FUNCTIONS
# ============================================

def register_client_config(config: VendorConfig):
    """
    Register a new client configuration at runtime
    """
    CLIENT_VENDOR_CONFIGS[config.client_id] = config
