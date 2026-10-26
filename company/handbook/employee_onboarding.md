# SOP: New Employee Onboarding

Owner: People Ops · IT owner: Raj Patel (IT Administrator)

## When to use
Requests such as "onboard <name>", "set up <name> who joins on <date>", "new hire".

## Required information
| Field | Notes |
|---|---|
| Full name | First and last name |
| Job title | e.g. "Data Analyst" |
| Department | One of the portal departments |
| Manager | Must be an existing employee in the portal |
| Start date | Portal format `YYYY-MM-DD` |
| Location | Bangalore, New York, London or Remote. Default to the manager's location if not stated. |
| Personal email | **Required.** Used to send pre-start credentials. Never guess it — if it was not provided, ask the requester. |

Work email is always generated as `firstname.lastname@acme-corp.com` (lowercase).

## Procedure
1. Check the person does not already exist on the portal **Employees** page.
2. Create the employee in the portal (**Employees → Add employee**).
3. Open IT tickets in the portal (**IT Tickets → Open new ticket**) for the new employee according to the
   equipment matrix below: one **Hardware** ticket for the laptop, and one **Software access** ticket per
   software item. Priority **High** if the start date is within 7 days of today, otherwise **Normal**.
4. Email the manager (internal address from the Employees page) a welcome note: name, start date, and the
   ticket numbers opened.
5. Save an onboarding checklist to `reports/onboarding_<firstname>_<lastname>.md` with what was done.

## Equipment matrix
| Role contains | Laptop (Hardware ticket item) | Software access tickets |
|---|---|---|
| Analyst | MacBook Pro 14 (16GB) | Tableau; Snowflake read access |
| Engineer | MacBook Pro 16 (32GB) | GitHub; AWS dev account |
| Manager | MacBook Air 13 | Workday manager role |
| (anything else) | Dell Latitude 7450 | Microsoft 365 |

## Done means
- Employee record exists with correct manager, department, start date and work email.
- One Hardware ticket and the correct Software access tickets exist for the employee.
- Manager has been emailed; checklist file saved.
