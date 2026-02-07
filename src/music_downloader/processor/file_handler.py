"""
File processor for renaming and placing downloaded files.
Handles the final step: rename to 'Artist - Title.flac' and move to output directory.
"""

import logging
import os
import re
import shutil

logger = logging.getLogger(__name__)


class FileProcessor:
    """Handles file renaming, moving, and cleanup."""

    def __init__(self, download_dir: str, output_dir: str, filename_template: str = "{artist} - {title}"):
        self.download_dir = download_dir
        self.output_dir = output_dir
        self.filename_template = filename_template

        # Ensure output directory exists
        os.makedirs(self.output_dir, exist_ok=True)
        logger.info(f"File processor initialized: downloads={download_dir}, output={output_dir}")

    def build_filename(self, artist: str, title: str, extension: str = "flac") -> str:
        """
        Build the target filename from artist and title.

        Args:
            artist: Artist/group name.
            title: Song title.
            extension: File extension (default: flac).

        Returns:
            Sanitized filename like 'Artist - Title.flac'.
        """
        name = self.filename_template.format(artist=artist, title=title)
        name = self._sanitize_filename(name)
        return f"{name}.{extension}"

    def find_downloaded_file(self, username: str, remote_filename: str) -> str | None:
        """
        Find a downloaded file on disk based on the slskd download structure.
        slskd stores downloads as: <download_dir>/<username>/<directory>/<filename>

        Args:
            username: The Soulseek username of the source.
            remote_filename: The remote path (with backslashes).

        Returns:
            Absolute path to the file on disk, or None if not found.
        """
        # The basename of the remote file
        basename = remote_filename.rsplit("\\", 1)[-1] if "\\" in remote_filename else remote_filename

        # Walk the download directory looking for the file
        user_dir = os.path.join(self.download_dir, username)
        if os.path.isdir(user_dir):
            for root, _, files in os.walk(user_dir):
                if basename in files:
                    path = os.path.join(root, basename)
                    logger.info(f"Found downloaded file: {path}")
                    return path

        # Fallback: search entire download directory
        for root, _, files in os.walk(self.download_dir):
            if basename in files:
                path = os.path.join(root, basename)
                logger.info(f"Found downloaded file (fallback): {path}")
                return path

        logger.warning(f"Downloaded file not found: {basename} (user={username})")
        return None

    def process_file(self, source_path: str, artist: str, title: str) -> str | None:
        """
        Rename and move a downloaded file to the output directory.

        Args:
            source_path: Path to the downloaded file.
            artist: Artist name for the filename.
            title: Song title for the filename.

        Returns:
            Path to the final file in the output directory, or None on failure.
        """
        try:
            if not os.path.isfile(source_path):
                logger.error(f"Source file does not exist: {source_path}")
                return None

            # Determine extension from source
            _, ext = os.path.splitext(source_path)
            extension = ext.lstrip(".").lower() or "flac"

            # Build target filename
            target_name = self.build_filename(artist, title, extension)
            target_path = os.path.join(self.output_dir, target_name)

            # Avoid overwriting existing files
            if os.path.exists(target_path):
                logger.warning(f"File already exists: {target_path}")
                # Add a numeric suffix
                base, ext_with_dot = os.path.splitext(target_path)
                counter = 1
                while os.path.exists(target_path):
                    target_path = f"{base} ({counter}){ext_with_dot}"
                    counter += 1

            # Copy (not move) to preserve the original in downloads
            # until user confirms. Then we can clean up.
            shutil.copy2(source_path, target_path)
            logger.info(f"File placed: {target_path}")

            return target_path

        except Exception:
            logger.exception(f"Failed to process file: {source_path}")
            return None

    def cleanup_download(self, source_path: str) -> bool:
        """
        Remove the original downloaded file after successful processing.

        Args:
            source_path: Path to the downloaded file to remove.

        Returns:
            True if cleanup succeeded.
        """
        try:
            if os.path.isfile(source_path):
                os.remove(source_path)
                logger.info(f"Cleaned up: {source_path}")

                # Also remove the parent directory if empty
                parent = os.path.dirname(source_path)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
                    logger.debug(f"Removed empty directory: {parent}")

                return True
            return False

        except Exception:
            logger.exception(f"Failed to cleanup: {source_path}")
            return False

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """
        Remove or replace characters that are invalid in filenames.

        Args:
            name: Raw filename string.

        Returns:
            Sanitized filename safe for all major filesystems.
        """
        # Replace characters invalid on Windows/Linux/macOS
        name = re.sub(r'[<>:"/\\|?*]', "", name)
        # Replace multiple spaces with single space
        name = re.sub(r"\s+", " ", name)
        # Strip leading/trailing whitespace and dots
        name = name.strip(" .")
        return name
