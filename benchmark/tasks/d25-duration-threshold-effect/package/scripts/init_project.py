"""
Initialize a new video project with standard folder structure.
Creates project.json template and storyboard.md.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime
VALID_VEO_DURATIONS = [4, 6, 8]

def snap_to_valid_duration(duration: int, threshold: int=7) -> int:
    """Snap a duration to the nearest valid Veo duration (4, 6, or 8 seconds)."""
    if duration <= 5:
        return 4
    elif duration <= 7:
        return 6
    else:
        return 8
DEFAULT_PROJECT = {'name': '', 'description': '', 'created': '', 'duration_target': 30, 'aspect_ratio': '16:9', 'resolution': '720p', 'audio_strategy': 'custom', 'scenes': [{'id': 1, 'name': 'scene1_intro', 'prompt': 'Describe the visual for this scene...', 'duration': 6, 'notes': ''}], 'voiceover': {'enabled': True, 'text': 'Write the voiceover script here...', 'voice': 'Charon', 'style': 'Professional, warm, engaging'}, 'music': {'enabled': True, 'prompt': 'Describe the music style...', 'duration': 35, 'bpm': 100, 'brightness': 0.5}, 'assembly': {'transition': 'fade', 'transition_duration': 0.5, 'music_volume': 0.3, 'fade_in': 1.0, 'fade_out': 2.0}}
DEFAULT_STORYBOARD = '# {project_name} - Storyboard\n\n## Overview\n**Duration Target:** {duration}s\n**Aspect Ratio:** {aspect_ratio}\n**Style:** [Describe the overall style]\n\n---\n\n## Scene Breakdown\n\n### Scene 1: [Title] (0-6s)\n**Visual:** [Describe what we see]\n**Audio:** [Music only / Voiceover: "..."]\n**Notes:** [Any special effects, transitions]\n\n### Scene 2: [Title] (6-12s)\n**Visual:** [Describe what we see]\n**Audio:** [Voiceover: "..."]\n**Notes:** []\n\n### Scene 3: [Title] (12-18s)\n**Visual:** [Describe what we see]\n**Audio:** [Voiceover: "..." + music swells]\n**Notes:** []\n\n---\n\n## Voiceover Script\n\n> [Write the complete voiceover script here.\n> This will be used for TTS generation.]\n\n---\n\n## Music Direction\n\n- **Style:** [e.g., Modern electronic, cinematic, upbeat]\n- **Energy:** [Low / Medium / High]\n- **Key moments:** [e.g., "Build at 15s, resolve at end"]\n\n---\n\n## Technical Notes\n\n- [ ] Audio strategy: custom (strip Veo audio, add VO + music)\n- [ ] Transitions: fade (0.5s)\n- [ ] Resolution: 720p\n'

def init_project(name: str, output_dir: str=None, duration: int=30, aspect_ratio: str='16:9', audio_strategy: str='custom', num_scenes: int=3) -> dict:
    """Initialize a new video project.
    
    Args:
        name: Project name (used for folder)
        output_dir: Parent directory (defaults to current dir)
        duration: Target video duration in seconds
        aspect_ratio: Video aspect ratio (16:9, 9:16, 1:1)
        audio_strategy: "veo_audio", "custom", or "silent"
        num_scenes: Number of scene placeholders to create
    
    Returns:
        dict with project info and paths
    """
    safe_name = name.lower().replace(' ', '_').replace('-', '_')
    safe_name = ''.join((c for c in safe_name if c.isalnum() or c == '_'))
    if output_dir:
        project_path = Path(output_dir) / safe_name
    else:
        project_path = Path.cwd() / safe_name
    if project_path.exists():
        return {'error': f'Project already exists: {project_path}'}
    try:
        folders = ['scenes', 'audio', 'work', 'output']
        for folder in folders:
            (project_path / folder).mkdir(parents=True, exist_ok=True)
        project_config = DEFAULT_PROJECT.copy()
        project_config['name'] = name
        project_config['created'] = datetime.now().isoformat()
        project_config['duration_target'] = duration
        project_config['aspect_ratio'] = aspect_ratio
        project_config['audio_strategy'] = audio_strategy
        raw_scene_duration = duration // num_scenes
        scene_duration = snap_to_valid_duration(raw_scene_duration)
        scenes = []
        for i in range(num_scenes):
            scenes.append({'id': i + 1, 'name': f'scene{i + 1}', 'prompt': f'Describe scene {i + 1} visual...', 'duration': scene_duration, 'notes': ''})
        project_config['scenes'] = scenes
        actual_duration = scene_duration * num_scenes
        if actual_duration != duration:
            print(f'⚠️  Note: Scene durations adjusted to {scene_duration}s each (Veo requires 4, 6, or 8s)')
            print(f'    Actual video length: ~{actual_duration}s (requested: {duration}s)')
        project_config['music']['duration'] = duration + 5
        project_file = project_path / 'project.json'
        with open(project_file, 'w') as f:
            json.dump(project_config, f, indent=2)
        storyboard_content = DEFAULT_STORYBOARD.format(project_name=name, duration=duration, aspect_ratio=aspect_ratio)
        storyboard_file = project_path / 'storyboard.md'
        with open(storyboard_file, 'w') as f:
            f.write(storyboard_content)
        gitignore_file = project_path / '.gitignore'
        with open(gitignore_file, 'w') as f:
            f.write('work/\n*.wav\n*.mp3\n*.mp4\n!output/*.mp4\n')
        return {'success': True, 'project_path': str(project_path), 'project_file': str(project_file), 'storyboard_file': str(storyboard_file), 'folders': {'scenes': str(project_path / 'scenes'), 'audio': str(project_path / 'audio'), 'work': str(project_path / 'work'), 'output': str(project_path / 'output')}}
    except Exception as e:
        return {'error': f'Failed to create project: {e}'}

def main():
    parser = argparse.ArgumentParser(description='Initialize a new video project', formatter_class=argparse.RawDescriptionHelpFormatter, epilog='\nExamples:\n  # Create a new 30-second product video project\n  python init_project.py --name "Product Launch Video" --duration 30\n  \n  # Create project in specific directory\n  python init_project.py --name "Demo Video" --output ~/Videos/projects/\n  \n  # Create vertical video for social\n  python init_project.py --name "Instagram Reel" --aspect-ratio 9:16 --duration 15\n  \n  # Create project with Veo\'s native audio\n  python init_project.py --name "Cinematic Scene" --audio-strategy veo_audio\n\nAfter creation:\n  1. Edit project.json - fill in scene prompts, voiceover text, music style\n  2. Edit storyboard.md - plan your video structure\n  3. Run: python assemble.py --project /path/to/project/\n        ')
    parser.add_argument('--name', '-n', required=True, help='Project name')
    parser.add_argument('--output', '-o', help='Parent directory for project folder')
    parser.add_argument('--duration', '-d', type=int, default=30, help='Target video duration in seconds (default: 30)')
    parser.add_argument('--aspect-ratio', '-a', default='16:9', choices=['16:9', '9:16', '1:1', '4:3'], help='Video aspect ratio (default: 16:9)')
    parser.add_argument('--audio-strategy', default='custom', choices=['custom', 'veo_audio', 'silent'], help='Audio strategy (default: custom)')
    parser.add_argument('--scenes', '-s', type=int, default=3, help='Number of scene placeholders (default: 3)')
    args = parser.parse_args()
    print(f'🎬 Initializing video project: {args.name}')
    result = init_project(args.name, args.output, args.duration, args.aspect_ratio, args.audio_strategy, args.scenes)
    if 'error' in result:
        print(f'❌ Error: {result['error']}', file=sys.stderr)
        sys.exit(1)
    else:
        print(f'✅ Project created!')
        print(f'\n📁 Project folder: {result['project_path']}')
        print(f'\n📄 Files created:')
        print(f'   • project.json - Edit scene prompts, voiceover, music settings')
        print(f'   • storyboard.md - Plan your video structure')
        print(f'\n📂 Folders:')
        print(f'   • scenes/  - Generated video clips go here')
        print(f'   • audio/   - Voiceover and music files')
        print(f'   • work/    - Intermediate files (auto-cleaned)')
        print(f'   • output/  - Final video output')
        print(f'\n🎯 Next steps:')
        print(f'   1. Edit project.json with your scene prompts and settings')
        print(f'   2. Run: python assemble.py --project {result['project_path']}')
if __name__ == '__main__':
    main()
