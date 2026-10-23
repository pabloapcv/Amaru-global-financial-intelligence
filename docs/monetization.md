# Selling ATLAS: an honest assessment

*Written 2026-10-22, at commit 17ca8d2. This is a product and business view, not legal advice. Confirm the licensing and regulatory points with a lawyer before charging anyone.*

## Short answer

You can sell it: it is your software, built on permissively licensed open-source components (MIT, BSD, Apache), and Anthropic's commercial terms generally give you rights to output produced for you (confirm the terms that apply to your account). Selling it **as it is today** is another matter. The obstacles are mostly data licensing, regulation and the lack of real users, not the code.

## What is worth something

- **Verified answers.** Every number in a research answer traces back to stored data or a calculation, every citation resolves, and all 8 of the spec's performance and quality targets pass (`atlas benchmark`). Few AI finance tools can say that, and it is the main thing to sell.
- **Business-software foundations already in place.** Separate workspaces, teams with roles and invitations, two-factor sign-in (which owners can require), an activity log, quotas, API keys, shareable report links and monthly Claude spending caps.
- **Analysis few competitors offer:**
  - USDA supply-and-demand balances, with a price-versus-stocks model;
  - CFTC positioning;
  - Latin American trade flows (for example Bolivia–US);
  - Fed and U.S. data calendars and official press releases.

## What blocks selling it today

### 1. Data licensing (the biggest issue)

| Source | Commercial use |
|---|---|
| USDA, CFTC, Federal Reserve, BEA | U.S. government data: generally fine |
| World Bank | CC BY 4.0: fine with attribution |
| FRED | Mixed. Some series belong to third parties (S&P 500, VIX, ICE benchmarks) and redistributing them can need the owner's permission. Check each series' notes. |
| IMF commodity prices (via FRED) | Check the IMF's terms for commercial redistribution |
| UN Comtrade | The free API tier may not allow commercial use; there is a paid tier |
| Exchange data (CME, ICE): futures, real-time | Not connected. Licences cost thousands per month and would change the pricing completely |

ATLAS now records each dataset's licence class and owner (Data Explorer, provenance, export headers). Setting `ATLAS_COMMERCIAL_MODE=true` switches off the three restricted series so a paid deployment does not redistribute them; "check terms" sources (IMF, UN Comtrade) still need a decision from you. The classification is a best-effort reading of each source's terms as of October 2026, not legal advice.

### 2. The data is public and delayed

Commodity prices are monthly IMF averages; for example, sugar's latest value was from July in October. Professional traders already pay for Bloomberg or Refinitiv and won't pay for lagged public data. The realistic buyer is someone who doesn't have a terminal.

### 3. Regulation

- General, impersonal research published to everyone usually sits outside investment-adviser rules. In the U.S. this is the "publisher's exclusion".
- Personalized recommendations ("buy this") are different and can require registration as an adviser.
- Rules differ by country. Have a lawyer review it for each market you sell into.
- Also review Anthropic's usage policy on financial use cases.

### 4. Nobody has used it

ATLAS was built in roughly a week and has 136 automated tests and passing CI, but no customers. Real users will find gaps that tests don't.

### 5. Missing commercial basics

- Billing: the Stripe integration is now built (Checkout, Customer Portal, verified webhooks) and only needs your Stripe account, prices and webhook set up
- Production hosting
- Terms of service and a privacy policy (the product side of data rights is built: people can download their data and delete their account, which privacy laws such as GDPR, Brazil's LGPD and California's CCPA expect)
- Backups
- Support
- A security review

## Where to aim it

Don't compete with Bloomberg, Koyfin or TradingView. Pick a niche where public data is enough and the value is turning it into a finished, verified analysis.

1. **Agricultural trade intelligence for Latin American exporters** (research briefs and reports can now be written in Spanish or Portuguese with Claude), importers, cooperatives, trade associations and the banks that finance them: sugar, coffee, soybeans and corn, plus flows like Bolivia–US. The data it relies on most is government data that allows commercial use. Plausible price: **$200–1,000 per month per company**.
2. **A paid weekly newsletter or report service** produced with ATLAS. It has the lowest regulatory and licensing risk, and it is the fastest way to test whether anyone will pay.
3. **University licences** for teaching finance and economics with verified analysis.
4. **An API for fintechs** that want a verified AI research engine inside their own product. This comes later, once the data licensing for redistribution is settled.

Selling the codebase itself without customers would probably fetch little. Paying users are what make it valuable.

## Costs and margin (rough)

- **Claude:** a research run costs roughly $0.20–1 at most. The plans already cap monthly Claude spend per workspace: Explorer $2, Investor $15, Research Pro $60, Pro $300.
- **Example:** a $49/month plan with a $15 Claude cap leaves roughly a **60% gross margin** after modest hosting ($50–200/month at the start).
- **Licensed exchange data** would add thousands per month in fixed cost. Only take that on once revenue covers it.

## Next steps

1. Talk to 15–20 people in the chosen niche before building more features.
2. Get 3–5 of them to pay for a pilot, even at a discount, as design partners.
3. Then settle the data licences for that niche, set up billing and production hosting, and get terms of service and a privacy policy written.

Still blocked on decisions or accounts from you:

- a Stripe account (then set the `STRIPE_*` settings; the integration is built)
- a choice of hosting
- an EIA API key (energy data)
- a licensed futures data source, if you need one
