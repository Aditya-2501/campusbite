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
- Persistent browser cart and mock paid checkout
- Paginated and filterable order history
- Validated order lifecycle transitions, delivery assignment, and customer cancellation
- Reviews after delivery and one return/refund request per order
- Owner restaurant menu CRUD, order dashboard, stats, reminder generation, and refund decisions
- Partner-only assigned deliveries and delivery history
- Customer scoped Gemini order insights using that customer's database history
- Request method/path/status/duration logging and a response time header

## Run the backend with Docker Compose

1. Copy `.env.example` to `.env` and replace the database password and JWT secret. A Gemini API key is required to use customer order insights.
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

Use the registration form to create an account, choosing Customer, Restaurant Owner, or Delivery Partner. Owner accounts receive a starter restaurant. The sample restaurants are browseable without signing in. Passwords are hashed and authentication tokens are stored in localStorage.

## Environment variables

See `.env.example`. `POSTGRES_PASSWORD` and `JWT_SECRET` must be changed for deployed environments. `GEMINI_API_KEY` enables order insights and stays on the backend. `GEMINI_MODEL` defaults to `gemini-3.8-flash`. Gemini free-tier model access and quotas depend on Google's current terms; free-tier prompts may be used to improve Google products, so use test order data for the demo. See the [Gemini API pricing page](https://ai.google.dev/gemini-api/docs/pricing) before deployment. Never commit `.env` files or real credentials.

## API overview

The REST API is rooted at `/api`; OpenAPI/Swagger documentation is served at `/docs`.

- `POST /auth/register`, `POST /auth/login`, `GET /auth/me`
- `GET /restaurants`, `GET /restaurants/{id}`
- `POST /orders`, `GET /orders`, `GET /orders/{id}`, `PATCH /orders/{id}/status`
- `POST /orders/{id}/review`, `POST /orders/{id}/returns`
- `GET /delivery/partners`, `PATCH /orders/{id}/assign`
- `GET/POST /owner/menu`, `PATCH /owner/menu/{id}`, `GET /owner/dashboard`, `GET /owner/returns`, `PATCH /owner/returns/{id}`
- `POST /insights` (customer only)

Protected endpoints use `Authorization: Bearer <JWT>`. Backend checks enforce roles, ownership, assignment, and lifecycle transitions. Order and menu lists support pagination/filtering where relevant. Health check: `GET /api/health`.

## Notes

The mock checkout records a `mock_paid` payment ledger entry and does not charge a card. Refund approval records a separate refund entry. Owner reminders are generated as a background task when their dashboard is opened, for PLACED or ACCEPTED orders idle more than 15 minutes. Production deployment should supply a managed database, a strong secret, HTTPS, and an explicit CORS origin list.
