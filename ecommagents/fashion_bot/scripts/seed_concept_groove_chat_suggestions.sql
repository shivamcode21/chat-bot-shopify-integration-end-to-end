INSERT INTO client_configs (client_id, config_key, config_value)
VALUES (
    'c3ffcb1b-afb9-4ca4-8746-a06698bec870'::uuid,
    'chat_suggestions_config',
    '{
      "home": [
        { "text": "🔥 Trending now", "message": "Show me what is trending now" },
        { "text": "🆕 New arrivals", "message": "Show me the latest new arrivals" },
        { "text": "💰 Best deals", "message": "Show me the best deals available" },
        { "text": "👗 Browse collections", "message": "Help me browse your collections" }
      ],
      "collection": [
        { "text": "🔥 Best sellers", "message": "Show me the best sellers in this collection" },
        { "text": "💰 On sale", "message": "Show me products on sale in this collection" },
        { "text": "🆕 New arrivals", "message": "Show me new arrivals in this collection" },
        { "text": "📏 Size filter", "message": "Help me filter this collection by size" }
      ],
      "cart": [
        { "text": "📦 Delivery time?", "message": "What is the delivery time for my cart?" },
        { "text": "💳 Payment options", "message": "What payment options are available?" },
        { "text": "🔄 Return policy", "message": "What is your return policy?" },
        { "text": "🎁 Any coupons?", "message": "Do you have any coupons I can use?" }
      ],
      "product": {
        "default": [
          { "text": "📏 Size chart", "message": "Show me the size chart" },
          { "text": "🧵 What fabric?", "message": "What fabric is this made of?" },
          { "text": "📦 Delivery time?", "message": "What is the delivery time?" },
          { "text": "💰 Any offers?", "message": "Do you have any offers on this product?" }
        ],
        "hoodie": [
          { "text": "📏 Size chart", "message": "Show me the hoodie size chart" },
          { "text": "🧵 Fabric details", "message": "Tell me the hoodie fabric details" },
          { "text": "🌡️ How warm?", "message": "How warm is this hoodie?" },
          { "text": "🧼 Wash care", "message": "What is the wash care for this hoodie?" }
        ],
        "denim": [
          { "text": "📏 Size guide", "message": "Show me the denim size guide" },
          { "text": "📐 Fit type?", "message": "What is the fit type of these denims?" },
          { "text": "🧵 Material?", "message": "What material are these denims made of?" },
          { "text": "📦 When delivered?", "message": "When will these denims be delivered?" }
        ],
        "tshirt": [
          { "text": "📏 Size chart", "message": "Show me the t-shirt size chart" },
          { "text": "🧵 Fabric?", "message": "What fabric is this t-shirt made of?" },
          { "text": "📦 Delivery?", "message": "What is the delivery time for this t-shirt?" },
          { "text": "🎨 Other colors?", "message": "Does this t-shirt come in other colors?" }
        ],
        "jacket": [
          { "text": "📏 Size chart", "message": "Show me the jacket size chart" },
          { "text": "🧵 Material?", "message": "What material is this jacket made of?" },
          { "text": "🌧️ Waterproof?", "message": "Is this jacket waterproof?" },
          { "text": "📦 Delivery?", "message": "What is the delivery time for this jacket?" }
        ],
        "shoes": [
          { "text": "📏 Size guide", "message": "Show me the shoe size guide" },
          { "text": "👟 True to size?", "message": "Are these shoes true to size?" },
          { "text": "🧹 Care tips", "message": "How should I care for these shoes?" },
          { "text": "📦 Delivery?", "message": "What is the delivery time for these shoes?" }
        ]
      },
      "postQuery": {
        "size": [
          { "text": "🛒 Add to cart", "message": "Help me add this to cart" },
          { "text": "📦 Delivery time?", "message": "What is the delivery time?" },
          { "text": "🔄 Return policy", "message": "What is your return policy?" }
        ],
        "fabric": [
          { "text": "📏 Size chart", "message": "Show me the size chart" },
          { "text": "🧼 Care instructions", "message": "What are the care instructions?" },
          { "text": "🛒 Add to cart", "message": "Help me add this to cart" }
        ],
        "delivery": [
          { "text": "🛒 Add to cart", "message": "Help me add this to cart" },
          { "text": "💳 Payment options", "message": "What payment options are available?" },
          { "text": "📞 Track order", "message": "Track my order" }
        ],
        "price": [
          { "text": "💰 Any coupons?", "message": "Do you have any coupons available?" },
          { "text": "🛒 Add to cart", "message": "Help me add this to cart" },
          { "text": "📦 Delivery?", "message": "What is the delivery time?" }
        ],
        "default": [
          { "text": "🛒 Add to cart", "message": "Help me add this to cart" },
          { "text": "📦 Delivery?", "message": "What is the delivery time?" },
          { "text": "📞 Contact support", "message": "I want to contact support" }
        ]
      }
    }'::jsonb
)
ON CONFLICT (client_id, config_key)
DO UPDATE SET config_value = EXCLUDED.config_value;
