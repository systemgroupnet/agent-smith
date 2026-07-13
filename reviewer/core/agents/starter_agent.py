"""
Starter Agent - File Classification Agent

This agent classifies files from merge requests and extracts metadata
such as file type, module name, package name, and Jira task number.

For frontend files (.ts, .html, .scss), it directly classifies by extension.
For other files, it delegates to the LLM-based starter agent.
"""

import os
import re
import time
from typing import Optional

from core.agents.base_agent import StructuredOutputAgent
from core.prompts import get_starter_agent_prompt
from core.workflow.state import StarterResponse


# Create the starter agent instance (for non-frontend files).
# Uses the default LLM (DEFAULT_LLM_MODEL) — no per-agent model override, so it
# can't hit an account guardrail on a provider-specific model like the rest of
# the reviewers.
_starter_agent = StructuredOutputAgent(
    system_prompt=get_starter_agent_prompt(),
    temperature=0.0,
    output_model=StarterResponse
)


class FileClassifier:
    """
    File classifier that handles both extension-based and LLM-based classification.
    """
    
    # Extension to file type mapping
    EXTENSION_MAP = {
        '.ts': 'typescript',
        '.html': 'html',
        '.scss': 'scss',
        '.css': 'scss',  # Treat CSS files as SCSS
    }
    
    @staticmethod
    def get_file_extension(file_path: str) -> str:
        """Extract file extension from file path."""
        return os.path.splitext(file_path)[1].lower()
    
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
        Extract module name from file path by splitting on '/' and taking the second item.
        
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
    def classify_by_extension(input_text: str, file_path: str) -> Optional[StarterResponse]:
        """
        Classify file based on extension for frontend files (.ts, .html, .scss).
        
        Args:
            input_text: Input text containing file information
            file_path: Path to the file being classified
            
        Returns:
            StarterResponse if file is a frontend file, None otherwise
        """
        extension = FileClassifier.get_file_extension(file_path)
        file_type = FileClassifier.EXTENSION_MAP.get(extension)
        
        if not file_type:
            return None
        
        # Extract fields from input text
        file_name = os.path.basename(file_path)
        content = FileClassifier.extract_multiline_field(input_text, 'content')
        diff = FileClassifier.extract_multiline_field(input_text, 'diff')
        target_branch = FileClassifier.extract_field_from_input(input_text, 'target_branch')
        source_branch = FileClassifier.extract_field_from_input(input_text, 'source_branch')
        jira_task_number = FileClassifier.extract_jira_task_number(input_text)
        module_name = FileClassifier.extract_module_name(file_path)
        
        # For frontend files, package is not applicable
        return StarterResponse(
            file_name=file_name,
            file_type=file_type,
            package_name="N/A",
            module_name=module_name,
            content=content,
            diff=diff,
            target_branch=target_branch,
            source_branch=source_branch,
            jira_task_number=jira_task_number
        )
    
    @staticmethod
    async def classify(input_text: str, file_path: str) -> StarterResponse:
        """
        Classify file using extension-based or LLM-based approach.
        
        Args:
            input_text: Input text containing file information
            file_path: Path to the file being classified
            
        Returns:
            StarterResponse with classification results
        """
        # Try extension-based classification first
        result = FileClassifier.classify_by_extension(input_text, file_path)
        
        if result:
            print(f"  📁 Classified by extension: {result.file_type}")
            return result
        
        # Fall back to LLM-based classification for other files
        print(f"  🤖 Using LLM for classification...")
        return await _starter_agent.run(input_text)


# Create a wrapper class that mimics the agent interface
class StarterAgentWrapper:
    """Wrapper that provides the same interface as the original starter_agent."""
    
    async def run(self, input_text: str, file_path: Optional[str] = None) -> StarterResponse:
        """
        Run classification using the appropriate method.
        
        Args:
            input_text: Input text containing file information
            file_path: Optional file path (will be extracted from input_text if not provided)
            
        Returns:
            StarterResponse with classification results
        """
        start_time = time.time()
        
        # Extract file_path from input_text if not provided
        if not file_path:
            file_path = FileClassifier.extract_field_from_input(input_text, 'file_path')
        
        if not file_path:
            # No file path available, use LLM classification
            result = await _starter_agent.run(input_text)
        else:
            result = await FileClassifier.classify(input_text, file_path)
        
        elapsed_time = time.time() - start_time
        print(f"⏱️  File classification completed in {elapsed_time:.2f}s")
        
        return result


# Export the wrapper as starter_agent
starter_agent = StarterAgentWrapper()

