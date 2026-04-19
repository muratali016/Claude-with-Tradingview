"""
Local config — paste your Gemini API key here.

DO NOT commit this file to git. Add `config.py` to your .gitignore.
Get a key at https://aistudio.google.com/apikey
"""

API_KEY = "zzz"

# Model to use. Options:
#   "gemma-4-26b-a4b-it"  — open MoE, fast/cheap, weaker at fine chart detail
#   "gemini-2.5-pro"      — stronger vision, better for dense charts
#   "gemini-2.5-flash"    — middle ground
MODEL = "gemini-2.5-flash"