# CampusBite

CampusBite is a campus restaurant ordering and delivery app. Customers can find restaurants, browse menus, check out with a mock payment, follow orders, leave reviews, request refunds, and ask AI questions about their own order history. Restaurant owners manage menu items and incoming orders; delivery partners see and update only their assigned deliveries.

## Stack

- React 18, JavaScript, React Router, Vite
- FastAPI, Pydantic, SQLAlchemy 2
- PostgreSQL in Docker Compose (SQLite is available for a quick local backend)
- JWT bearer authentication and bcrypt password hashing
- Gemini API via Google's official Python SDK for customer order insights

## Features

- Role based access for customer, owner, and delivery accounts, enforced by backend dependencies
- Restaurant discovery and price/name menu filtering
- Restaurant ratings calculated from completed customer reviews, with unrated listings marked as new
- Persistent browser cart and mock paid checkout
- Paginated and filterable order history
- Validated order lifecycle transitions, delivery assignment, and customer cancellation
- Reviews after delivery and one return/refund request per order
- Owner menu CRUD (create, edit, availability, and delete), order dashboard, stats, reminders, and refund decisions
- Owner invitation codes and claiming an unassigned sample restaurant for local demonstrations
- Partner-only assigned deliveries and delivery history
- Customer scoped Gemini order insights using that customer's database history
- Request method/path/status/duration logging and a response time header

## Run the backend with Docker Compose

1. Copy `.env.example` to `.env`, set strong unique database and JWT secrets, and add a Gemini API key for customer order insights. Owner and delivery registration require the matching invitation code configured in `.env`.
2. Start the API and PostgreSQL:

   ```sh
   docker compose up --build
   ```

3. The API is at `http://localhost:8000`; Swagger documentation is at `http://localhost:8000/docs`.
4. Stop services with `docker compose down`. Add `-v` only if you want to delete the local database volume.

Tables are created automatically at startup. A small set of sample restaurants and dishes are seeded into a fresh database. SQLite can be used outside Docker by running `cd backend`, installing `requirements.txt`, then `uvicorn app.main:app --reload`; the default database is `backend/campusbite.db`.

## Run the frontend locally

In the project root:

```sh
npm install
npm run dev
```

The development UI is at `http://localhost:5173`. It calls `http://localhost:8000/api` by default. To use a different API URL, create a root `.env.local` with `VITE_API_URL=https://your-api.example/api`. If you change the UI origin, add it to `CORS_ORIGINS` in the backend environment.

The matching local frontend variable is listed in `.env.example.frontend`.

## Accounts and roles

Anyone can register as a customer. Owner and delivery accounts require `OWNER_REGISTRATION_CODE` or `DELIVERY_REGISTRATION_CODE`; only share those values with trusted testers. New owners get a starter kitchen and can claim an unassigned sample restaurant if the starter has no menu or orders. This makes the seeded restaurant order flow demonstrable without allowing customers to order from an unowned kitchen. Deleting a dish keeps its name and price in completed order history. The sample restaurants are browseable without signing in. Passwords are hashed and authentication tokens are stored in localStorage; the browser keeps carts separated by account.

## Environment variables

See `.env.example`. Use `APP_ENV=production` for a deployment; startup then rejects a short/default JWT secret and wildcard CORS. Configure explicit trusted origins. `POSTGRES_PASSWORD` and `JWT_SECRET` must be unique and strong. `GEMINI_API_KEY` enables order insights and stays on the backend. Per-customer AI requests are limited by `GEMINI_REQUESTS_PER_MINUTE` (default 5 per minute); login attempts are limited per client address. These in-memory limits are per backend process and need a shared store for multi-worker deployments. `GEMINI_MODEL` defaults to `gemini-3.5-flash-lite`. Order history is sent to Google to generate answers. Gemini free-tier model access, quotas, and data terms may change; use test order data for the demo. See the [Gemini API pricing page](https://ai.google.dev/gemini-api/docs/pricing) before deployment. Never commit `.env` files or real credentials.

## API overview

The REST API is rooted at `/api`; OpenAPI/Swagger documentation is served at `/docs`.

- `POST /auth/register`, `POST /auth/login`, `GET /auth/me`
- `GET /restaurants`, `GET /restaurants/{id}`
- `POST /orders`, `GET /orders`, `GET /orders/{id}`, `PATCH /orders/{id}/status`
- `POST /orders/{id}/review`, `POST /orders/{id}/returns`
- `GET /delivery/partners`, `PATCH /orders/{id}/assign`
- `GET/POST /owner/menu`, `PATCH/DELETE /owner/menu/{id}`, `GET /owner/dashboard`, `GET /owner/returns`, `PATCH /owner/returns/{id}`
- `POST /insights` (customer only)

Protected endpoints use `Authorization: Bearer <JWT>`. Backend checks enforce roles, ownership, assignment, and lifecycle transitions. Order and menu lists support pagination/filtering where relevant. Health check: `GET /api/health`.

## Notes

The mock checkout records a `mock_paid` payment ledger entry and does not charge a card. Refund approval records a separate refund entry. Owner reminders are generated when their dashboard is opened, for PLACED or ACCEPTED orders idle more than 15 minutes. PostgreSQL startup converts legacy floating-point money columns to `NUMERIC(12, 2)`, rounding existing values to cents; back up the database before deploying schema-changing updates. The backend container runs as a non-root user. Production deployment should supply a managed database, strong secrets, HTTPS, an explicit CORS origin list, shared rate limiting, and a separately hosted frontend. Payment remains a mock checkout and does not charge a card.
