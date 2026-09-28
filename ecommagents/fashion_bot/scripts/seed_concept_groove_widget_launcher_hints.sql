INSERT INTO client_configs (client_id, config_key, config_value)
VALUES (
    'c3ffcb1b-afb9-4ca4-8746-a06698bec870'::uuid,
    'widget_launcher_hints',
    '{
      "default": [
        "What is your latest collection?",
        "Show me some trending styles",
        "Do you have white shirts?",
        "Suggest something for a Goa trip",
        "Track my order"
      ],
      "byClientId": {
        "c3ffcb1b-afb9-4ca4-8746-a06698bec870": [
          "Show me Concept Groove bestsellers",
          "What is your latest collection?",
          "Suggest a vacation outfit",
          "Do you have white shirts?",
          "Track my order"
        ]
      },
      "byClientName": {
        "Concept Groove": [
          "Show me Concept Groove bestsellers",
          "What is your latest collection?",
          "Suggest a vacation outfit",
          "Do you have white shirts?",
          "Track my order"
        ]
      },
      "byDomainSubstring": {
        "conceptgroove": [
          "Show me Concept Groove bestsellers",
          "What is your latest collection?",
          "Suggest a vacation outfit",
          "Do you have white shirts?",
          "Track my order"
        ]
      }
    }'::jsonb
)
ON CONFLICT (client_id, config_key)
DO UPDATE SET config_value = EXCLUDED.config_value;
