"""
Static File Classifier

This module provides static file classification based on file names and patterns.
No LLM is used - classification is done purely based on file name matching.
"""

import os
import re
from typing import Optional


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
        """
        Classify file type based on file name and extension.
        
        Classification rules:
        - *.sql or db.go -> sql
        - dataview.go -> dataview
        - *_test.go or ts.go or *_ts.go -> test
        - pbook.go -> pbook
        - *.ts -> typescript
        - *.html -> html
        - *.scss or *.css -> scss
        - service.go or validation.go or any other .go file -> service
        
        Args:
            file_path: Path to the file
            
        Returns:
            File type as string
        """
        file_name = os.path.basename(file_path)
        file_name_lower = file_name.lower()
        
        # Get extension
        _, ext = os.path.splitext(file_name)
        ext_lower = ext.lower()
        
        # SQL files
        if ext_lower == '.sql' or file_name_lower == 'db.go':
            return 'sql'
        
        # Dataview files
        if file_name_lower == 'dataview.go':
            return 'dataview'
        
        # Test files
        if file_name_lower.endswith('_test.go') or file_name_lower == 'ts.go' or file_name_lower.endswith('_ts.go'):
            return 'test'
        
        # Pbook files
        if file_name_lower == 'pbook.go':
            return 'pbook'
        
        # TypeScript files
        if ext_lower == '.ts':
            return 'typescript'
        
        # HTML files
        if ext_lower == '.html':
            return 'html'
        
        # SCSS/CSS files
        if ext_lower in ['.scss', '.css']:
            return 'scss'
        
        # Service files (service.go, validation.go, or any other .go file)
        if ext_lower == '.go':
            return 'service'

        # Anything else is not a file any reviewer here understands.
        #
        # This used to fall through to 'service' — the **Go** reviewer — so a
        # Makefile, an .svg, a .json or a .proto was handed to two Go agents
        # (semantic + business logic), burning two LLM calls to produce findings
        # about a language the file isn't written in. The only thing standing
        # between that and the merge request was REVIEW_EXCLUDE_PATTERNS, i.e. a
        # denylist in someone's .env masking a broken default: whatever nobody had
        # thought to list went to Go anyway.
        #
        # 'unknown' has no reviewer, so such a file is skipped and counted as
        # skipped. Adding a genuinely new reviewable type is a deliberate change
        # here plus a routing entry, which is the right amount of friction.
        return 'unknown'
