"""
Static File Classifier

This module provides static file classification based on file names and patterns.
No LLM is used - classification is done purely based on file name matching.
"""

import re

from core.config.review_profile import get_profile


class StaticFileClassifier:
    """
    Static file classifier that classifies files based on file name patterns.
    """
    
    @staticmethod
    def extract_jira_task_number(input_text: str) -> str:
        """
        Extract Jira task number from input text.
        Looks for patterns like PROJ-123 or other common Jira formats.
        """
        # Common Jira pattern: [A-Z]+-\d+
        match = re.search(r'\b([A-Z]{2,10}-\d+)\b', input_text)
        if match:
            return match.group(1)
        return "N/A"
    
    @staticmethod
    def extract_field_from_input(input_text: str, field_name: str) -> str:
        """
        Extract a field value from input text.
        Format: field_name: value
        """
        pattern = rf'{field_name}:\s*(.+?)(?:\n|$)'
        match = re.search(pattern, input_text, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""
    
    @staticmethod
    def extract_multiline_field(input_text: str, field_name: str) -> str:
        """
        Extract a multiline field value from input text.
        Format: field_name:\nvalue\n\nnext_field:
        """
        pattern = rf'{field_name}:\s*\n(.*?)(?:\n\n|\n[a-z_]+:|\Z)'
        match = re.search(pattern, input_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""
    
    @staticmethod
    def extract_module_name(file_path: str) -> str:
        """
        Extract module name from file path by taking the first directory.
        
        Args:
            file_path: Path to the file (e.g., 'src/module_name/file.py')
            
        Returns:
            Module name as string, or "N/A" if path doesn't have enough segments
        """
        parts = file_path.split('/')
        if len(parts) >= 2:
            return parts[0]
        return "N/A"
    
    @staticmethod
    def classify_file(file_path: str) -> str:
        """Classify a file by the active review profile's ``file_types`` rules.

        The rules — which names and extensions are SQL, tests, services — used to
        be hard-coded here for one codebase. They now live in YAML
        (``REVIEW_PROFILE``, see `core/config/review_profile.py`), first match
        wins, and a file matching no rule is ``unknown``: skipped and counted as
        skipped. There is deliberately no catch-all default — the old one handed
        every Makefile and ``.svg`` to the Go reviewers.
        """
        return get_profile().classify(file_path)
