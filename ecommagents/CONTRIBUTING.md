# Contributing to EcommAgents

Thanks for contributing.

## Before you start
1. Read the project README.
2. Do not commit secrets, customer/order data, local databases, `.env` files, service-account keys, or generated artifacts.
3. Keep changes focused and document externally visible API or integration changes.

## Development
~~~bash
cd ecommagents/fashion_bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
~~~

Run the API:
~~~bash
python -m fashion_bot.main
~~~

Run relevant tests:
~~~bash
pytest
~~~

## Pull requests
Include what changed, why it changed, how it was tested, new environment variables or external services, and migration/compatibility notes when applicable.