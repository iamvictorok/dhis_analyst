# DHIS2 AI Analyst: Comprehensive Test Suite

Use this guide to test every single capability, output mode, and API routing strategy of the DHIS2 AI Analyst. You can copy and paste these directly into the frontend chat interface.

## 1. Casual Chat & System Info
*Tests the AI's ability to skip DHIS2 data fetching (`requires_data: false`) and act as a conversational assistant.*
- "Hello! Who are you?"
- "What capabilities do you have?"
- "Can you explain what DHIS2 is?"

## 2. Standard Analytics & Dashboards
*Tests the core `analytics_api` strategy, relative period parsing, and chart generation.*
- "Show me the trend of Malaria confirmed cases in Bo for the last 6 months."
- "Compare ANC 1st visit and ANC 4th or more visits in Kenema for this year."
- "What were the total OPV3 coverage numbers for last quarter?"

## 3. Disaggregations (Phase 1)
*Tests the LLM's ability to extract the `ou_children` token and dynamically break down data geographically.*
- "Compare Malaria confirmed cases **across all districts** in Sierra Leone for last year."
- "Show me a breakdown of HIV testing **by clinic** in Bo district."
- "What is the spatial distribution of Covid-19 cases **by region**?"

## 4. Tracker API & PII Anonymization (Phase 2)
*Tests the `tracker_api` strategy, line-list generation, and the Zero-Trust Tokenization middleware.*
- "Show me a line list of individual patients who tested positive for Malaria in Bo."
- "What is the date of previous malaria episode for the recent cases?"
- "Can you pull the specific patient records for the recent maternal mortality cases?"

## 5. Data Quality & Raw Values (Phase 3)
*Tests the `data_value_sets` strategy, bypassing the analytics engine to inspect un-aggregated data entry.*
- "Are there any data entry errors or suspiciously high values for Malaria cases in Bo this week?"
- "Show me the raw un-aggregated data for HIV testing to check for missing daily reports."
- "Validate the raw data values entered at the clinic level yesterday."

## 6. Geospatial Mapping (Phase 4)
*Tests the `map` output mode and the HTML GeoJSON rendering engine.*
- "Show me a heat map of Malaria cases across all districts this year."
- "Can you plot the geographical distribution of HIV testing in Bo?"

## 7. Demographic & Category Disaggregations (Phase 5)
*Tests the `metadata_resolve` category resolution and `analytics` API dimension building.*
- "Show me Malaria cases by age group and gender in Bo for the last 6 months."
- "Compare HIV testing by facility type across all districts."
- "Give me a breakdown of maternal mortality by age."
- "Generate a map showing the Covid-19 spread across the national level."

## 7. Ambiguity & Clarification Handling
*Tests the AI's ability to push back when a query lacks a health metric.*
- "Show me the data for Bo district last year."
- "Can you compare the districts in Sierra Leone?"
- *(The AI should politely ask: "What public health question or indicator should I analyze?")*

## 8. Formal Reports & Web Context
*Tests the `report` output mode and the ability to fetch external context from the web.*
- "Write a formal programme review report on Malaria confirmed cases in Bo for this year. Include WHO benchmarks and external context."
- "Generate a briefing document on the latest OPV3 coverage trends."

## 9. Data Export
*Tests the `export` output mode to generate raw data payloads.*
- "I need to download the raw data for ANC 1st visits in Western Area for the last 12 months."
- "Extract the Malaria data into an excel format."

## 10. Conversational Follow-ups (Memory)
*Tests the LLM's ability to remember the context of the previous query and only change specific parameters.*
- **Step 1:** "Show me the trend of Malaria cases in Bo for last year."
- **Step 2:** "What about for this year?"
- **Step 3:** "Change the location to Kenema."





Query	Tests
"Show me Malaria confirmed cases in Bo for the last 6 months"	Token-AND search, exact indicator
"How many kids got their shots in Bo last year?"	Synonym expansion (shots→immunization), layperson language
"Compare ANC 1st visit and ANC 4th or more visits in Kenema"	Multi-metric, exact-ish indicator names
"Hello! Who are you?"	Chitchat bypass (no data pipeline)
"Give me malaria deaths across all districts"	Synonym + ou_children disaggregation