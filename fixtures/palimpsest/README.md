# Palimpsest synthetic fixture

Synthetic contact-card inbox for the JIT-style experiment. The agent must:

1. Discover every `.vcf` card in `contact_cards/`
2. Exclude any card whose full name is exactly "Tom" (Tom is excluded from the workbook)
3. Sort the remaining cards alphabetically by full name
4. Build `contact.xlsx` with columns `name`, `email`, `phone` (one row per remaining card)
5. NOT send anything (no email step — this is a synthetic experiment)

The `verify_contact_workbook.py` script is the acceptance gate: it fails unless
`contact.xlsx` exists, contains exactly the non-Tom cards, and is sorted.
