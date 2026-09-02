# ATS Configuration Guide

Career Radar queries public job endpoints directly from employer applicant tracking systems (ATS). This guide explains how to find the required configuration parameters for each supported platform in your `~/.config/career-radar/employers.yaml`.

---

## 1. Greenhouse

Greenhouse boards are identified by a single `board` token found in the public job board URL.

* **URL Pattern:** `https://boards.greenhouse.io/<board_token>` or `https://job-boards.greenhouse.io/<board_token>`
* **Example:** For `https://boards.greenhouse.io/openai`, the board token is `openai`.

**Configuration:**
```yaml
- name: OpenAI
  ats: greenhouse
  board: openai
```

---

## 2. Lever

Lever boards are identified by the company slug in the URL.

* **URL Pattern:** `https://jobs.lever.co/<company_slug>`
* **Example:** For `https://jobs.lever.co/figma`, the company slug is `figma`.

**Configuration:**
```yaml
- name: Figma
  ats: lever
  company: figma
```

---

## 3. Ashby

Ashby boards are identified by the board slug in the URL.

* **URL Pattern:** `https://jobs.ashbyhq.com/<board_slug>`
* **Example:** For `https://jobs.ashbyhq.com/linear`, the board slug is `linear`.

**Configuration:**
```yaml
- name: Linear
  ats: ashby
  board: linear
```

---

## 4. Workday

Workday career sites are structured around three elements:
1. `host`: The domain of the Workday careers site (e.g. `stripe.wd1.myworkdayjobs.com`).
2. `tenant`: The company tenant identifier in the URL path.
3. `site`: The career site path identifier (commonly `careers`, `Stripe_Careers`, etc.).

* **URL Pattern:** `https://<host>/wday/cxs/<tenant>/<site>/jobs` or `https://<host>/en-US/<tenant>/<site>`
* **How to find it:** Navigate to the company's careers site. Open your browser's Developer Tools (Network tab), filter for `jobs` or `cxs`, and observe the POST request made when searching listings.

**Configuration:**
```yaml
- name: Stripe
  ats: workday
  host: stripe.wd1.myworkdayjobs.com
  tenant: stripe
  site: careers
```

---

## 5. Rippling

Rippling ATS pages use a company slug.

* **URL Pattern:** `https://ats.rippling.com/<slug>/jobs`
* **Example:** If the board is `https://ats.rippling.com/acme-corp/jobs`, the slug is `acme-corp`.

**Configuration:**
```yaml
- name: Acme Corp
  ats: rippling
  slug: acme-corp
```

---

## 6. Paylocity

Paylocity recruiting boards use a company GUID token.

* **URL Pattern:** `https://recruiting.paylocity.com/Recruiting/Jobs/All/<company_id>`
* **Example:** The company ID is the UUID at the end of the URL.

**Configuration:**
```yaml
- name: Example Co
  ats: paylocity
  company_id: 12345678-abcd-1234-abcd-1234567890ab
```

---

## 7. JazzHR

JazzHR boards use the company subdomain slug.

* **URL Pattern:** `https://<slug>.applytojob.com/apply`

**Configuration:**
```yaml
- name: Example Co
  ats: jazzhr
  slug: examplecompany
```

---

## 8. Breezy HR

Breezy boards use the portal subdomain.

* **URL Pattern:** `https://<slug>.breezy.hr`

**Configuration:**
```yaml
- name: Example Co
  ats: breezy
  slug: examplecompany
```

---

## 9. iCIMS

iCIMS portals use the tenant subdomain.

* **URL Pattern:** `https://<subdomain>.icims.com/jobs/search`

**Configuration:**
```yaml
- name: Example Corp
  ats: icims
  subdomain: careers-example
```

---

## 10. USAJOBS (Federal Government)

Queries official United States Federal government job listings via the USAJOBS Search API.

* **Prerequisites:** Requires a free API key from the [USAJOBS Developer Portal](https://developer.usajobs.gov/). Set `export USAJOBS_API_KEY="your-key"`.
* **User-Agent:** USAJOBS requires your registered developer email address as the `User-Agent`. Set `export USAJOBS_USER_AGENT="your-email@domain.com"`.

**Configuration:**
```yaml
- name: Federal US Jobs
  ats: usajobs
  location: Remote
```

---

## 11. Adzuna (Job Aggregator)

Queries listings via the Adzuna API across various roles and geographies.

* **Prerequisites:** Requires an App ID and App Key from the [Adzuna Developer Portal](https://developer.adzuna.com/). Set `export ADZUNA_APP_ID="your-id"` and `export ADZUNA_APP_KEY="your-key"`.

**Configuration:**
```yaml
- name: Tech Jobs in NY
  ats: adzuna
  where: New York, NY
  what: Python Developer OR Data Engineer
```

---

## 12. Universal LLM Scraper

For companies with custom career sites that don't use standard ATS software, Career Radar can scrape the page content and extract structured listings using your configured LLM.

**Configuration:**
```yaml
- name: Custom Company
  ats: universal_llm
  url: https://example.com/careers
```
