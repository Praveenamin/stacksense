import requests
import json
import re
from django.conf import settings


class OllamaAnalyzer:
    """Analyzes anomalies using Ollama LLM to generate human-readable explanations."""
    
    def __init__(self):
        self.api_url = getattr(settings, "OLLAMA_API_URL", "http://localhost:11434")
        self.model = getattr(settings, "OLLAMA_MODEL", "llama3.2")
        self.timeout = getattr(settings, "OLLAMA_TIMEOUT", 120)
        self.enabled = getattr(settings, "LLM_ENABLED", True)
    
    def _call_ollama(self, prompt):
        """Make a request to Ollama API."""
        if not self.enabled:
            return None
        
        try:
            response = requests.post(
                f"{self.api_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False
                },
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json().get("response", "")
        except requests.exceptions.Timeout:
            raise Exception(f"LLM timeout: Request took longer than {self.timeout} seconds")
        except Exception as e:
            raise Exception(f"Ollama API error: {e}")
    
    def explain_anomaly(self, metric_type, metric_name, metric_value, server_name):
        """
        Generate a human-readable explanation for a detected anomaly.
        Returns a string explanation or None if LLM is disabled.
        """
        if not self.enabled:
            return None
        
        prompt = f"""A system monitoring tool detected an anomaly on server "{server_name}".

Metric Type: {metric_type}
Metric Name: {metric_name}
Current Value: {metric_value}

Provide a brief, human-readable explanation (2-3 sentences) of:
1. What this anomaly means in practical terms
2. What might be causing it
3. What the system administrator should check first

Keep it concise and actionable. Return only the explanation text, no JSON or formatting."""

        try:
            response = self._call_ollama(prompt)
            if response:
                # Clean up response
                response = response.strip()
                # Remove any markdown formatting
                response = re.sub(r"```.*?```", "", response, flags=re.DOTALL)
                response = re.sub(r"`", "", response)
                return response
        except Exception as e:
            print(f"Failed to generate LLM explanation: {e}")
            return None
        
        return None
