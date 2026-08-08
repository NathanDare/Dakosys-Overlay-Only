#!/usr/bin/env python3
"""
TV/Anime Status Tracker Module for DAKOSYS

This module tracks TV show statuses and creates Kometa overlays and Trakt lists
for airing episodes, season finales, and other special events.
"""

import os
import sys
import json
import yaml
import time
import logging
import requests
import pytz
from datetime import datetime
from plexapi.server import PlexServer
from rich.console import Console
from tmdb_helper import get_show, should_refresh

console = Console()

logger = logging.getLogger("tv_status_tracker")

class TVStatusTracker:
    """TV and Anime Status Tracker for DAKOSYS."""

    def __init__(self, config):
        """Initialize with DAKOSYS configuration."""
        self.config = config

        self.data_dir = "data"
        if os.environ.get('RUNNING_IN_DOCKER') == 'true':
            self.data_dir = "/app/data"

        os.makedirs(self.data_dir, exist_ok=True)

        self.setup_logging()

        self.plex_url = config['plex']['url']
        self.plex_token = config['plex']['token']

        self.libraries = []
        plex_libs = config['plex'].get('libraries', {})
        self.libraries.extend(plex_libs.get('anime', []))
        self.libraries.extend(plex_libs.get('tv', []))

        self.timezone = config['timezone']


        self.tmdb_api_key = config.get('tmdb', {}).get('api_key')

        self.tv_status_config = config['services']['tv_status_tracker']
        self.colors = self.tv_status_config.get('colors', {})

        _default_labels = {
            'ended': 'ENDED',
            'cancelled': 'CANCELLED',
            'returning': 'RETURNING',
            'airing': 'AIRING',
            'season_finale': 'FINALE',
            'mid_season_finale': 'MID FINALE',
            'final_episode': 'FINAL EPISODE',
            'season_premiere': 'RETURNS',
        }
        self.labels = {**_default_labels, **self.tv_status_config.get('labels', {})}
        self.yaml_output_dir = config.get('kometa_config', {}).get('yaml_output_dir', '/kometa/config/overlays')
        self.collections_dir = config.get('kometa_config', {}).get('collections_dir', '/kometa/config/collections')

        font_path = self.tv_status_config.get('font_path')
        if not font_path or not os.path.exists(font_path):
            kometa_config = os.path.dirname(self.collections_dir)
            fallback_path = os.path.join(kometa_config, "overlays/fonts", "AvenirNextLTPro-Bold.ttf")
    
            if os.path.exists(fallback_path):
                font_path = fallback_path
            elif os.path.exists('/app/fonts/Juventus-Fans-Bold.ttf'):
                font_path = '/app/fonts/Juventus-Fans-Bold.ttf'
            else:
                logger.warning(f"Font not found. Using system default.")
                font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

        self.font_path = font_path
        kometa_conf = self.config.get('kometa_config', {})
        self.overlay_config = self.tv_status_config.get('overlay', {})

        logger.debug(f"Overlay config loaded: {self.overlay_config}")
        font_path_from_get = self.overlay_config.get('font_path')
        logger.debug(f"Font path from get: '{font_path_from_get}' (type: {type(font_path_from_get)})") 
        self.font_path_yaml = "config/overlays/fonts/AvenirNextLTPro-Bold.ttf"
        #if not self.font_path_yaml:
        #    font_dir = kometa_conf.get('font_directory', 'config/fonts')
        #    font_name = self.overlay_config.get('font_name', 'Juventus-Fans-Bold.ttf')
        #    self.font_path_yaml = os.path.join(font_dir, font_name)

        asset_dir = kometa_conf.get('asset_directory', 'config/assets')
        gradient_name = self.overlay_config.get('gradient_name', 'gradient_top.png')
        self.gradient_image_path_yaml = os.path.join(asset_dir, gradient_name)
        
        #logger.info(f"Using font for script (fallback logic): {self.font_path}")
        #logger.info(f"Using font for Kometa YAML: {self.font_path_yaml}")
        #logger.info(f"Using gradient for Kometa YAML: {self.gradient_image_path_yaml}")

        self.airing_shows = []



        self.overlay_style = self.overlay_config.get('overlay_style', 'background_color')
        self.apply_gradient_background = self.overlay_config.get('apply_gradient_background', False)


        self.yaml_file_template = "overlay_tv_status_{library}.yml"

    def setup_logging(self):
        """Set up logging for the TV Status Tracker."""
        os.makedirs(self.data_dir, exist_ok=True)

        log_file = os.path.join(self.data_dir, "tv_status_tracker.log")

        from logging.handlers import RotatingFileHandler

        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG)

        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        # Use UTF-8 for the file handler so Unicode titles can be written safely
        handler = RotatingFileHandler(
            log_file,
            maxBytes=5*1024*1024,
            backupCount=3,
            encoding='utf-8'
        )
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        # Also add a console handler that respects UTF-8 (for interactive runs)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        logging.debug("TV Status Tracker started.")

    def sanitize_title_for_search(self, title):
        safe_title = title  
    
        if "'" in safe_title:  
            safe_title = safe_title.replace("'", "%'%")
    
        if "," in safe_title:
            safe_title = safe_title.replace(",", ",%")
    
        if "&" in safe_title:
            safe_title = safe_title.replace("&", "%&%")
    
        if ":" in safe_title:
            safe_title = safe_title.replace(":", "%:%")
        
        if "/" in safe_title:
            safe_title = safe_title.replace("/", "%/%")
    
        logging.debug(f"Sanitized title for search (no leading %): '{safe_title}' from original '{title}'")
        return safe_title

    def process_show(self, show):
        # Extract TMDB ID from Plex GUIDs
        tmdb_id = None
        for guid in show.guids:
            if 'tmdb://' in guid.id:
                tmdb_id = guid.id.split('//')[1]
                break
        if not tmdb_id:
            logging.warning(f"No TMDB ID for show {show.title}")
            return None

        # Load cached entry
        entry = self.local_db.get(tmdb_id, {})
        cached_status = entry.get('status')
        if not should_refresh(entry, cached_status):
            logging.info(f"Skipping TMDB refresh for {show.title} (tmdb_id={tmdb_id}); last checked {entry.get('last_checked')}")
            status_type = entry.get('status_type', 'UNKNOWN')
            return {
                'text_content': entry.get('text_content', 'UNKNOWN'),
                'back_color': self.colors.get(status_type, '#E9E9E9'),
                'font': self.font_path_yaml,
                'status_type': status_type
            }

        # Fetch fresh data from TMDB
        tmdb_data = get_show(tmdb_id, self.tmdb_api_key)
        if not tmdb_data:
            logging.error(f"TMDB request failed for {show.title} (tmdb_id={tmdb_id})")
            return None

        status = tmdb_data.get('status', '').lower()
        text_content = 'UNKNOWN'
        back_color = self.colors.get(status.upper(), '#E9E9E9')
        status_type = 'UNKNOWN'

        if status == 'ended':
            text_content = 'ENDED'
            back_color = self.colors.get('ENDED', back_color)
            status_type = 'ENDED'
        elif status in ('canceled', 'cancelled'):
            text_content = 'CANCELLED'
            back_color = self.colors.get('CANCELLED', back_color)
            status_type = 'CANCELLED'
        elif status == 'returning series':
            next_ep = tmdb_data.get('next_episode_to_air')
            if next_ep and next_ep.get('air_date'):
                utc_time = datetime.strptime(next_ep['air_date'], '%Y-%m-%d')
                local_time = utc_time.replace(tzinfo=pytz.utc).astimezone(pytz.timezone(self.timezone))
                fmt = '%m/%d' if self.config.get('date_format', 'DD/MM').upper() == 'MM/DD' else '%d/%m'
                date_str = local_time.strftime(fmt)
                text_content = f"{self.labels.get('airing', 'AIRING')} {date_str}"
                back_color = self.colors.get('AIRING', back_color)
                status_type = 'AIRING'
                self.airing_shows.append({
                    'tmdb_id': tmdb_id,
                    'title': show.title,
                    'first_aired': next_ep['air_date'],
                    'episode_type': next_ep.get('episode_type', '')
                })
            else:
                text_content = 'RETURNING'
                back_color = self.colors.get('RETURNING', back_color)
                status_type = 'RETURNING'
        else:
            text_content = 'RETURNING'
            back_color = self.colors.get('RETURNING', back_color)
            status_type = 'RETURNING'

        # Update local DB entry
        self.local_db[tmdb_id] = {
            'title': show.title,
            'status': status,
            'date': date_str if 'date_str' in locals() else '',
            'next_air_date': next_ep['air_date'] if 'next_ep' in locals() and next_ep and next_ep.get('air_date') else '',
            'text_content': text_content,
            'status_type': status_type,
            'last_checked': datetime.utcnow().isoformat() + 'Z'
        }

        return {
            'text_content': text_content,
            'back_color': back_color,
            'font': self.font_path_yaml,
            'status_type': status_type
        }

    def create_yaml(self, library_name):
        """Create YAML overlay file for a library."""
        logging.info(f"Processing library: {library_name}")
        console.print(f"[bold blue]Processing library: {library_name}[/bold blue]")

        try:
            plex = PlexServer(self.plex_url, self.plex_token)
            library = plex.library.section(library_name)
            yaml_data = {'overlays': {}}

            for show in library.all():
                logging.debug(f"Processing {show.title}...")
                show_info = self.process_show(show)

                if show_info:
                    formatted_title = f"{show.title}_{show.year}".replace(' ', '_') if show.year else show.title.replace(' ', '_')

                    safe_title = self.sanitize_title_for_search(show.title)
                    logging.debug(f"Using sanitized title for search: '{safe_title}'")

                    plex_search_all = {'title.is': safe_title}
                    if show.year:
                        plex_search_all['year'] = show.year

                    yaml_data['overlays'][f'{library_name}_Status_{formatted_title}'] = {
                        'overlay': {
                            'back_color': '#00000000',
                            'font': 'config/overlays/fonts/AvenirNextLTPro-Bold.ttf',
                            'font_size': 66,
                            'font_color': show_info['back_color'],
                            'horizontal_align': 'center',
                            'horizontal_offset': 0,
                            'name': f"text({show_info['text_content']})",
                            'vertical_align': 'top',
                            'vertical_offset': 25,
                        },
                        'plex_search': {
                            'all': plex_search_all
                        }
                    }
                    logging.debug(f"Processed {show.title} with status {show_info['text_content']}.")

            yaml_file_path = os.path.join(self.yaml_output_dir, self.yaml_file_template.format(library=library_name.lower()))
            with open(yaml_file_path, 'w') as file:
                yaml.dump(yaml_data, file, allow_unicode=True, default_flow_style=False)

            logging.info(f'YAML file created for {library_name}: {yaml_file_path}')
            console.print(f"[green]YAML file created: {yaml_file_path}[/green]")

        except Exception as e:
            logging.error(f"Error processing library {library_name}: {str(e)}")
            console.print(f"[red]Error processing library {library_name}: {str(e)}[/red]")

    def sort_airing_shows_by_date(self):
        """Sort airing shows by air date."""
        return sorted(self.airing_shows, key=lambda x: datetime.strptime(x['first_aired'], '%Y-%m-%dT%H:%M:%S.000Z'))

    # Trakt list synchronization removed; TMDB data is stored locally.

    def run(self):
        """Run the TV Status Tracker using TMDB data and a local JSON DB."""
        console.print("[bold]Starting TV/Anime Status Tracker...[/bold]")

        if not os.path.exists(self.yaml_output_dir):
            console.print(f"[red]Error: YAML output directory does not exist: {self.yaml_output_dir}[/red]")
            logging.error(f"YAML output directory does not exist: {self.yaml_output_dir}")
            return False

        if not os.path.exists(self.collections_dir):
            console.print(f"[red]Error: Collections directory does not exist: {self.collections_dir}[/red]")
            logging.error(f"Collections directory does not exist: {self.collections_dir}")
            return False

        # Load previous status cache
        status_cache_file = os.path.join(self.data_dir, "tv_status_cache.json")
        previous_status = {}
        is_first_run = not os.path.exists(status_cache_file)
        try:
            if os.path.exists(status_cache_file):
                with open(status_cache_file, "r") as f:
                    previous_status = json.load(f)
        except Exception as e:
            logging.error(f"Error loading previous status cache: {str(e)}")

        # Load or initialise local TMDB DB
        db_path = os.path.join(self.data_dir, "local_tv_db.json")
        try:
            with open(db_path, "r") as f:
                self.local_db = json.load(f)
        except FileNotFoundError:
            self.local_db = {}
        except Exception as e:
            logging.error(f"Error loading local TV DB: {str(e)}")
            self.local_db = {}

        current_status = {}
        total_shows_processed = 0
        changes = {
            'AIRING': [],
            'SEASON_FINALE': [],
            'MID_SEASON_FINALE': [],
            'FINAL_EPISODE': [],
            'SEASON_PREMIERE': [],
            'RETURNING': [],
            'ENDED': [],
            'CANCELLED': [],
            'DATE_CHANGED': []
        }

        for library_name in self.libraries:
            try:
                plex = PlexServer(self.plex_url, self.plex_token)
                library = plex.library.section(library_name)
                yaml_data = {'overlays': {}}

                for show in library.all():
                    total_shows_processed += 1
                    logging.debug(f"Processing {show.title}...")
                    show_info = self.process_show(show)

                    if show_info:
                        text_parts = show_info['text_content'].split()
                        status_text = text_parts[0]

                        date_str = ''
                        for part in text_parts:
                            if '/' in part and any(c.isdigit() for c in part):
                                date_str = part
                                break

                        show_key = f"{show.title} ({show.year})" if show.year else show.title

                        current_status[show_key] = {
                            'status': status_text,
                            'date': date_str,
                            'text': show_info['text_content']
                        }

                        if show_key in previous_status:
                            prev = previous_status[show_key]
                            curr = current_status[show_key]

                            status_changed = prev['status'] != curr['status']
                            date_changed = prev['date'] != curr['date'] and curr['date']

                            if status_changed or date_changed:
                                logging.debug(f"Change detected for {show_key}: Status changed: {status_changed}, Date changed: {date_changed}")
                                logging.debug(f"Previous: {prev['status']} ({prev['date']}), Current: {curr['status']} ({curr['date']})")

                                status_key = None

                                if status_changed:
                                    status_key = show_info.get('status_type')
                                elif date_changed and not status_changed:
                                    status_key = 'DATE_CHANGED'

                                if status_key:
                                    changes[status_key].append({
                                        'title': show_key,
                                        'prev_status': prev['status'],
                                        'new_status': curr['status'],
                                        'prev_date': prev['date'],
                                        'new_date': curr['date'],
                                        'full_text': curr['text'],
                                        'library': library_name
                                    })
                        else:
                            curr = current_status[show_key]

                            if is_first_run:
                                status_key = show_info.get('status_type')

                                if status_key and (bool(curr['date']) or status_key == 'FINAL_EPISODE'):
                                    changes[status_key].append({
                                        'title': show_key,
                                        'prev_status': 'NEW',
                                        'new_status': curr['status'],
                                        'prev_date': '',
                                        'new_date': curr['date'],
                                        'full_text': curr['text'],
                                        'library': library_name
                                    })
                            else:
                                status_key = show_info.get('status_type')

                                if status_key:
                                    changes[status_key].append({
                                        'title': show_key,
                                        'prev_status': 'NEW',
                                        'new_status': curr['status'],
                                        'prev_date': '',
                                        'new_date': curr['date'],
                                        'full_text': curr['text'],
                                        'library': library_name
                                    })

                        formatted_title = f"{show.title}_{show.year}".replace(' ', '_') if show.year else show.title.replace(' ', '_')

                        safe_title = self.sanitize_title_for_search(show.title)
                        
                        overlay_details = {
                            'font': 'config/overlays/fonts/AvenirNextLTPro-Bold.ttf',
                            'font_size': 66,
                            'font_color': show_info['back_color'],
                            'back_color': '#00000000',
                            'horizontal_align': 'center',
                            'horizontal_offset': 0,
                            'name': f"text({show_info['text_content']})",
                            'vertical_align': 'top',
                            'vertical_offset': 25,
                        }

                        plex_search_all = {'title.is': safe_title}
                        if show.year:
                            plex_search_all['year'] = show.year
                        plex_search_block = {'all': plex_search_all}

                        status_overlay_key = f'{library_name}_Status_{formatted_title}'
                        yaml_data['overlays'][status_overlay_key] = {
                            'overlay': overlay_details,
                            'plex_search': plex_search_block
                        }

                        #if self.apply_gradient_background:
                        #    gradient_overlay_key = f'{library_name}_StatusGradient_{formatted_title}'
                        #    yaml_data['overlays'][gradient_overlay_key] = {
                        #        'overlay': {
                        #            'file': self.gradient_image_path_yaml,
                        #            'height': self.overlay_config.get('back_height', 90),
                        #            'horizontal_align': self.overlay_config.get('horizontal_align', "center"),
                        #            'horizontal_offset': self.overlay_config.get('horizontal_offset', 0),
                        #            'name': f'status_gradient_for_{formatted_title}',
                        #            'order': 10,
                        #            'vertical_align': self.overlay_config.get('vertical_align', "top"),
                        #            'vertical_offset': self.overlay_config.get('vertical_offset', 25),
                        #            'width': self.overlay_config.get('back_width', 1000)
                        #        },
                        #        'plex_search': plex_search_block
                        #    }
                        #    logging.debug(f"Added gradient layer for {show.title}")

                        #if self.overlay_style == 'colored_text':
                        #    text_overlay_key = f'{library_name}_StatusText_{formatted_title}'
                        #    text_overlay_details = {
                        #        'name': f"text({show_info['text_content']})",
                        #        'font': 'config/overlays/fonts/AvenirNextLTPro-Bold.ttf',
                        #        'font_size': self.overlay_config.get('font_size', 66),
                        #        'font_color': show_info['back_color'], 
                        #        'back_color': '#00000000', 
                        #        'horizontal_align': self.overlay_config.get('horizontal_align', 'center'),
                        #        'vertical_align': self.overlay_config.get('vertical_align', 'top'),
                        #        'horizontal_offset': self.overlay_config.get('horizontal_offset', 0),
                        #        'vertical_offset': self.overlay_config.get('vertical_offset', 25),
                        #        'back_width': self.overlay_config.get('back_width', 0),
                        #        'back_height': self.overlay_config.get('back_height', 0),
                        #        'order': 20 
                        #    }
                        #    yaml_data['overlays'][text_overlay_key] = {
                        #        'overlay': text_overlay_details,
                        #        'plex_search': plex_search_block
                        #    }
                        #    logger.info(f"Added text layer for {show.title} with status {show_info['text_content']}.")

                        #elif self.overlay_style == 'background_color':
                        #    overlay_key = f'{library_name}_Status_{formatted_title}'
                        #    overlay_details = {
                        #        'font': 'config/overlays/fonts/AvenirNextLTPro-Bold.ttf',
                        #        'font_size': self.overlay_config.get('font_size', 66),
                        #        'horizontal_align': self.overlay_config.get('horizontal_align', 'center'),
                        #        'horizontal_offset': self.overlay_config.get('horizontal_offset', 0),
                        #        'name': f"text({show_info['text_content']})",
                        #        'vertical_align': self.overlay_config.get('vertical_align', 'top'),
                        #        'vertical_offset': self.overlay_config.get('vertical_offset', 25),
                        #        'back_width': self.overlay_config.get('back_width', 0),
                        #        'back_height': self.overlay_config.get('back_height', 0),
                        #        'color': self.overlay_config.get('color', '#FFFFFF'), 
                        #        'back_color': show_info['back_color'] 
                        #    }
                        #    yaml_data['overlays'][overlay_key] = {
                        #        'overlay': overlay_details,
                        #        'plex_search': plex_search_block
                        #    }
                        #    logging.debug(f"Processed {show.title} with status {show_info['text_content']} (background_color style).")

                yaml_file_path = os.path.join(self.yaml_output_dir, self.yaml_file_template.format(library=library_name.lower()))
                with open(yaml_file_path, 'w', encoding='utf-8') as file:
                    yaml.dump(yaml_data, file, allow_unicode=True, default_flow_style=False)

                logging.info(f'YAML file created for {library_name}: {yaml_file_path}')
                console.print(f"[green]YAML file created: {yaml_file_path}[/green]")

            except Exception as e:
                logging.error(f"Error processing library {library_name}: {str(e)}")
                console.print(f"[red]Error processing library {library_name}: {str(e)}[/red]")

        # Create collection files
        #self.create_yaml_collections()
    
        # Trakt list update removed – TMDB is now source of truth.

        # Save updated local TV DB atomically
        db_path = os.path.join(self.data_dir, "local_tv_db.json")
        tmp_path = db_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding='utf-8') as f:
                json.dump(self.local_db, f, indent=2)
            os.replace(tmp_path, db_path)
        except Exception as e:
            logging.error(f"Error writing local TV DB: {str(e)}")

        # Save status cache as before
        try:
            with open(status_cache_file, "w", encoding='utf-8') as f:
                json.dump(current_status, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving status cache: {str(e)}")

        have_changes = any(len(shows) > 0 for status, shows in changes.items())
        if have_changes and not os.environ.get('QUIET_MODE') == 'true':
            try:
                from notifications import notify_tv_status_updates
                notify_tv_status_updates(changes, total_shows_processed)
                logging.info("Sent TV status notifications")
            except Exception as e:
                logging.error(f"Error sending TV status notifications: {str(e)}")

        console.print("[bold green]TV/Anime Status Tracker completed successfully[/bold green]")
        return True

def run_tv_status_tracker(config=None):
    """Run the TV Status Tracker as a standalone function."""
    if not config:
        config_path = "/app/config/config.yaml" if os.environ.get('RUNNING_IN_DOCKER') == 'true' else "config/config.yaml"
        try:
            with open(config_path, 'r') as file:
                config = yaml.safe_load(file)
        except Exception as e:
            print(f"Error loading configuration: {str(e)}")
            return False

    if not config.get('services', {}).get('tv_status_tracker', {}).get('enabled', False):
        print("TV/Anime Status Tracker is disabled in configuration.")
        return False

    tracker = TVStatusTracker(config)
    return tracker.run()

if __name__ == "__main__":
    run_tv_status_tracker()
