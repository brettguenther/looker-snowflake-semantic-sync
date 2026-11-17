"""Structural protocols for Snowflake Semantic View extraction and Looker LookML deployment."""

from typing import List, Optional, Protocol, Tuple
from snowflake_semantic_sync.models.snowflake import SnowflakeSemanticView


class SnowflakeSemanticSource(Protocol):
    """Protocol for extracting Snowflake Semantic Views."""

    def list_semantic_views(self, database: str, schema: str) -> List[str]:
        """Lists semantic view names in the specified Snowflake database and schema."""
        ...

    def get_semantic_views(
        self,
        database: str,
        schema: str,
        view_names: Optional[List[str]] = None,
    ) -> List[SnowflakeSemanticView]:
        """Extracts and parses Snowflake Semantic Views via SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW."""
        ...


class LookMLDeployer(Protocol):
    """Protocol for deploying and validating LookML files in Looker."""

    def ensure_dev_mode(self) -> None:
        ...

    def checkout_branch(self, project_id: str, branch: str = "master") -> None:
        ...

    def create_or_update_file(self, project_id: str, file_path: str, content: str) -> None:
        ...

    def validate_project(self, project_id: str) -> Tuple[bool, str]:
        ...
