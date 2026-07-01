"""
Static File Classifier

This module provides static file classification based on file names and patterns.
No LLM is used - classification is done purely based on file name matching.
"""

import os
import re
from typing import Optional
from core.workflow.state import StarterResponse


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
        
        # Default to service for unknown types
        return 'service'
    
    @staticmethod
    def classify(input_text: str, file_path: str) -> StarterResponse:
        """
        Classify file using static rules based on file name.
        
        Args:
            input_text: Input text containing file information
            file_path: Path to the file being classified
            
        Returns:
            StarterResponse with classification results
        """
        # Extract fields from input text
        file_name = os.path.basename(file_path)
        content = StaticFileClassifier.extract_multiline_field(input_text, 'content')
        diff = StaticFileClassifier.extract_multiline_field(input_text, 'diff')
        target_branch = StaticFileClassifier.extract_field_from_input(input_text, 'target_branch')
        source_branch = StaticFileClassifier.extract_field_from_input(input_text, 'source_branch')
        jira_task_number = StaticFileClassifier.extract_jira_task_number(input_text)
        module_name = StaticFileClassifier.extract_module_name(file_path)
        
        # Classify file type
        file_type = StaticFileClassifier.classify_file(file_path)
        
        # For most files, package is not applicable
        # Only service and dataview files might have packages
        package_name = "N/A"
        
        return StarterResponse(
            file_name=file_name,
            file_type=file_type,
            package_name=package_name,
            module_name=module_name,
            content=content,
            diff=diff,
            target_branch=target_branch,
            source_branch=source_branch,
            jira_task_number=jira_task_number
        )


# Create a wrapper class that mimics the agent interface
class StaticClassifierWrapper:
    """Wrapper that provides an async interface for static file classification."""
    
    async def run(self, input_text: str, file_path: Optional[str] = None) -> StarterResponse:
        """
        Run classification using static rules.
        
        Args:
            input_text: Input text containing file information
            file_path: Optional file path (will be extracted from input_text if not provided)
            
        Returns:
            StarterResponse with classification results
        """
        # Extract file_path from input_text if not provided
        if not file_path:
            file_path = StaticFileClassifier.extract_field_from_input(input_text, 'file_path')
        
        if not file_path:
            raise ValueError("file_path is required for static classification")
        
        result = StaticFileClassifier.classify(input_text, file_path)
        
        print(f"  📁 Classified statically as: {result.file_type}")
        
        return result


# Export the wrapper as static_classifier
static_classifier = StaticClassifierWrapper()
