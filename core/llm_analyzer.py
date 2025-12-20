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
    
    def explain_anomaly(self, metric_type, metric_name, metric_value, server_name, process_context=None):
        """
        Generate a human-readable explanation for a detected anomaly.
        
        Args:
            metric_type: Type of metric ('cpu', 'memory', etc.)
            metric_name: Name of the metric
            metric_value: Current value of the metric
            server_name: Name of the server
            process_context: Optional dict with process information
                Format: {'cpu': [{'pid': '12345', 'cpu_percent': 95.8, 'command': 'yes'}], 'memory': [...]}
        
        Returns:
            string explanation or None if LLM is disabled
        """
        if not self.enabled:
            return None
        
        prompt = f"""A system monitoring tool detected an anomaly on server "{server_name}".

Metric Type: {metric_type}
Metric Name: {metric_name}
Current Value: {metric_value}
"""
        
        # Add process information if available
        if process_context:
            relevant_processes = process_context.get('cpu' if metric_type == 'cpu' else 'memory', [])
            if relevant_processes:
                prompt += f"""
Process Information (collected at anomaly detection time):
Top {metric_type.upper()} Processes:
"""
                for proc in relevant_processes[:3]:  # Top 3
                    if metric_type == 'cpu':
                        prompt += f"  - PID {proc['pid']}: {proc['command']} ({proc['cpu_percent']}% CPU)\n"
                    else:
                        prompt += f"  - PID {proc['pid']}: {proc['command']} ({proc['memory_percent']}% Memory)\n"
                
                prompt += "\nAnalyze these specific processes to identify the root cause. Identify which process(es) are causing the anomaly and why."
        else:
            prompt += "\n(Process information not available - provide general analysis)"
        
        prompt += """

Provide a detailed explanation with two clearly labeled sections:

CAUSE:
Explain what is causing this anomaly. If process information is available above, identify specific processes (by name/PID) and explain what they are doing. Otherwise, explain general causes. Be specific about the root cause.

FIX:
Provide step-by-step instructions to resolve. If specific processes are identified above, include commands to investigate/kill them (e.g., "kill PID" or "ps aux | grep process_name"). Otherwise, provide general troubleshooting steps.

Format your response as:
CAUSE: [explanation]
FIX: [step-by-step instructions]

Keep it clear, concise, and actionable. Return only the explanation text with the CAUSE: and FIX: labels."""

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
