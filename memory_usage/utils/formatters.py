"""
Formatting utilities for the memory monitor
"""


def format_ledger_ranges(complete_ledgers: str, max_length: int = 50) -> str:
    """Format complete_ledgers string for display.
    
    If the string is too long, shows first and last range with count.
    E.g., "100-200,300-400,...,900-1000 (5 ranges)"
    """
    if not complete_ledgers or complete_ledgers == 'empty':
        return complete_ledgers
    
    if len(complete_ledgers) <= max_length:
        return complete_ledgers
    
    # Split into ranges
    ranges = [r.strip() for r in complete_ledgers.split(',')]
    if len(ranges) <= 2:
        return complete_ledgers
    
    # Show first and last with ellipsis
    return f"{ranges[0]},...,{ranges[-1]} ({len(ranges)} ranges)"


def format_duration(seconds: float) -> str:
    """Format duration in seconds to a readable format like 01m:30.5s or 01h:05m:30s"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes:02d}m:{secs:04.1f}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}h:{minutes:02d}m:{secs:02.0f}s"


def format_memory(memory_mb: float) -> str:
    """Format memory in MB to a readable format"""
    if memory_mb < 1024:
        return f"{memory_mb:.1f}MB"
    else:
        memory_gb = memory_mb / 1024
        return f"{memory_gb:.2f}GB"


def format_binary_name(binary_path: str) -> str:
    """Extract and format binary name from path"""
    from pathlib import Path
    return Path(binary_path).name