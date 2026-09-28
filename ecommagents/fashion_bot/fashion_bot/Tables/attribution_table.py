"""
Attribution Events Table
Stores chatbot attribution events for KPI calculation and conversion tracking.
"""

def create_attribution_events_table(cursor):
    """Create the chat_attribution_events table for tracking chatbot interactions."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_attribution_events (
            id SERIAL PRIMARY KEY,
            
            -- Session identifiers
            bot_ref VARCHAR(100) NOT NULL,           -- Attribution token (unique per chat session)
            anon_id VARCHAR(100) NOT NULL,           -- Anonymous user identifier (browser-scoped)
            session_id VARCHAR(100),                 -- Widget session ID
            client_id UUID,                          -- Client/store identifier
            
            -- Event details
            event_type VARCHAR(50) NOT NULL,         -- chat_opened, message_sent, product_clicked, link_clicked, cart_updated, checkout_started
            event_data JSONB DEFAULT '{}',           -- Additional event metadata
            
            -- Page context
            page_url TEXT,
            page_type VARCHAR(50),                   -- home, product, collection, cart, checkout
            product_handle VARCHAR(255),
            product_title TEXT,
            product_price VARCHAR(50),

            -- Device/Browser info
            user_agent TEXT,
            referrer TEXT,

            -- Identity recovery (filled in once the customer is phone-verified
            -- mid-chat; lets order webhooks find the session via phone when
            -- bot_ref/anon_id weren't carried through to checkout)
            phone_number VARCHAR(20),

            -- Links this event's session to the backend conversation record
            -- (messages + tags), so order attribution can confirm the chat
            -- was genuine pre-sales talk rather than a greeting or support query.
            conversation_id UUID,

            -- Timestamps
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

            -- Indexes for efficient queries
            CONSTRAINT valid_event_type CHECK (event_type IN (
                'chat_opened', 'chat_closed', 'message_sent', 'message_received',
                'product_viewed', 'product_clicked', 'link_clicked',
                'add_to_cart', 'cart_updated', 'checkout_started',
                'order_completed', 'session_started', 'session_ended'
            ))
        );

        -- Backfill columns for tables created before phone_number/conversation_id existed
        ALTER TABLE chat_attribution_events ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);
        ALTER TABLE chat_attribution_events ADD COLUMN IF NOT EXISTS conversation_id UUID;

        -- Index for bot_ref lookups (most common query)
        CREATE INDEX IF NOT EXISTS idx_attribution_bot_ref
        ON chat_attribution_events(bot_ref);

        -- Index for anon_id lookups (for assisted conversions)
        CREATE INDEX IF NOT EXISTS idx_attribution_anon_id
        ON chat_attribution_events(anon_id);

        -- Index for client_id + time range queries
        CREATE INDEX IF NOT EXISTS idx_attribution_client_time
        ON chat_attribution_events(client_id, created_at DESC);

        -- Index for event type filtering
        CREATE INDEX IF NOT EXISTS idx_attribution_event_type
        ON chat_attribution_events(event_type);

        -- Composite index for attribution window queries
        CREATE INDEX IF NOT EXISTS idx_attribution_conversion
        ON chat_attribution_events(anon_id, event_type, created_at DESC);

        -- Index for phone-based recovery lookups (webhook fallback path)
        CREATE INDEX IF NOT EXISTS idx_attribution_phone_client_time
        ON chat_attribution_events(client_id, phone_number, created_at DESC);

        -- Index for joining matched events to their conversation's messages/tags
        CREATE INDEX IF NOT EXISTS idx_attribution_conversation_id
        ON chat_attribution_events(conversation_id);
    """)
    print("✓ Created chat_attribution_events table")


def create_order_attribution_table(cursor):
    """Create the order_attribution table for linking orders to chat sessions."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_attribution (
            id SERIAL PRIMARY KEY,
            
            -- Order identifiers
            order_id VARCHAR(100) NOT NULL,          -- Shopify order ID
            order_number VARCHAR(50),                -- Human-readable order number
            client_id UUID NOT NULL,
            
            -- Attribution tokens (from cart/order attributes)
            bot_ref VARCHAR(100),                    -- Direct attribution token
            anon_id VARCHAR(100),                    -- For assisted attribution
            
            -- Attribution classification
            attribution_type VARCHAR(20) NOT NULL,   -- 'direct', 'assisted', 'none'
            attribution_window_hours INTEGER,        -- Hours between last chat and order
            
            -- Order details
            order_total DECIMAL(10, 2),
            order_currency VARCHAR(10) DEFAULT 'INR',
            order_items_count INTEGER,
            
            -- Chat session info (for direct attribution)
            chat_session_id VARCHAR(100),
            last_chat_event_at TIMESTAMP WITH TIME ZONE,
            chat_messages_count INTEGER,
            
            -- Timestamps
            order_created_at TIMESTAMP WITH TIME ZONE,
            attributed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            
            -- Unique constraint to prevent duplicate attributions
            CONSTRAINT unique_order_attribution UNIQUE (order_id, client_id)
        );
        
        -- Index for order lookups
        CREATE INDEX IF NOT EXISTS idx_order_attribution_order_id 
        ON order_attribution(order_id);
        
        -- Index for client + time range queries
        CREATE INDEX IF NOT EXISTS idx_order_attribution_client_time 
        ON order_attribution(client_id, order_created_at DESC);
        
        -- Index for attribution type filtering
        CREATE INDEX IF NOT EXISTS idx_order_attribution_type 
        ON order_attribution(attribution_type);
        
        -- Index for bot_ref lookups
        CREATE INDEX IF NOT EXISTS idx_order_attribution_bot_ref 
        ON order_attribution(bot_ref);
    """)
    print("✓ Created order_attribution table")


def create_attribution_summary_view(cursor):
    """Create a view for quick KPI calculations."""
    cursor.execute("""
        CREATE OR REPLACE VIEW attribution_kpi_summary AS
        SELECT 
            client_id,
            DATE(order_created_at) as order_date,
            attribution_type,
            COUNT(*) as order_count,
            SUM(order_total) as total_revenue,
            AVG(order_total) as avg_order_value,
            AVG(chat_messages_count) as avg_messages_per_order
        FROM order_attribution
        WHERE client_id IS NOT NULL
        GROUP BY client_id, DATE(order_created_at), attribution_type;
    """)
    print("✓ Created attribution_kpi_summary view")


def initialize_attribution_tables(cursor):
    """Initialize all attribution-related tables."""
    create_attribution_events_table(cursor)
    create_order_attribution_table(cursor)
    create_attribution_summary_view(cursor)
    print("✅ All attribution tables initialized")

