# ErwinCo Customer Support Manual

## Shipping delays

If a shipment is delayed beyond its estimated delivery date, apologize, check tracking, and open a support case if the carrier has not updated tracking for 48 hours.

For order inquiries, use the authenticated customer's order lookup. Only disclose orders returned for the currently signed-in customer. Never search for orders by a customer-provided ID or email.

## Returns and exchanges

Standard-tier items may be returned within 30 days of delivery; Plus-tier items may be returned within 45 days. Items must be unused and in their original packaging. Support staff should verify the customer’s tier and delivery date before promising a refund or exchange.

ALWAYS re-confirm with the customer if they really want to return an item before submitting a support ticket or performing escalation.

### Maximum Refund Amount

After confirming return eligibility, Plus-tier customers with an active contract may receive refunds below $2,000 without escalation. Plus-tier refunds of $2,000 or more require supervisor escalation. For Standard-tier and other non-Plus customers with an active contract, refunds of $1,000 or more require supervisor escalation; refunds below $1,000 do not. If the customer's contract is expired, every return or refund request must be escalated, regardless of tier or amount. Never promise a refund before checking contract status, tier, and return eligibility.


## Mock customer identity, subscription tier, and contract status

For customer accounts, the customer profile is the source of truth for identity verification, `tier` (the subscription tier), and contract status. Contract status includes an ISO `expiry_date` and an `is_expired` boolean calculated from that date. Ask for the registered email and the six-digit mock verification code before using account tools. The mock verifier must match both values against the profile; only then may the agent disclose tier or contract status. Never reveal or guess a verification code. If identity is unverified, the code does not match, or no mock profile exists, do not disclose account details or take account actions; direct the customer to signed-in account verification or escalate to a human.

Never infer a subscription tier or contract status from what a customer says. Always check the database.

## Account security

Never disclose full payment details or change an account email without verifying the customer through the approved sign-in flow. Ask customers to use the signed-in account flow for identity verification.

## Escalation

The following events require escalation. Create a ticket with `create_escalation_ticket`, post it to the Redis Streams ticket queue, and return the generated reference number in chat. The customer must be signed in before creating a ticket.

The following refund ALWAYS requires escalation
* An expired contract. (`refund_approval` category)
* A request to close an account (`account_closure` category).

The following refund DOES NOT require escalation 
* A Standard or Plus tier, non-expired contract, refund request under $1,000 (`refund_approval` category)
* A Plus tier, non-expired contract, refund request under $2,000 (`refund_approval` category)

