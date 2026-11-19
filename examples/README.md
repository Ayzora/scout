# Example configurations

## A single company

```bash
scout research --domain monzo.com \
  --objectives "What do they sell, who leads engineering, and what is their tech stack?"
```

## A batch from CSV

`companies.csv` needs a `domain` column; `name` is optional.

```bash
scout research --csv examples/companies.csv \
  --objectives "Identify their core products and their main European markets."
```

## Specific fields only

```bash
scout research --domain monzo.com \
  --fields "industry,company_size,headquarters_location,technology_signals"
```

## Qualifying against an ideal customer profile

```bash
scout research --csv examples/companies.csv \
  --objectives "Assess fit for our observability platform." \
  --icp '{"size":"200-5000 employees","region":"UK or EU","signals":"runs its own cloud infrastructure"}'
```

Then export:

```bash
scout export --run 1 -o results.csv
```
