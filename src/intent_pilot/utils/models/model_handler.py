import json
import os
import time
import uuid
from pathlib import Path

from PIL import Image

from intent_pilot.utils.config import Config
from intent_pilot.utils.encoding import encode_image
from intent_pilot.utils.img_utils import extract_element_bbox
from intent_pilot.utils.models.askui import get_labeled_image
from intent_pilot.utils.models.gpt4 import format_gpt4v_message, get_response_from_gpt4v
from intent_pilot.utils.models.ollama import format_ollama_message, get_response_from_ollama
from intent_pilot.utils.models.prompts import (
    get_user_first_message_prompt,
    get_user_prompt,
)
from intent_pilot.utils.screenshot import capture_screen_with_cursor
from intent_pilot.utils.terminal import ANSI_BRIGHT_GREEN, ANSI_RESET

config = Config()


def get_relative_user_prompt(messages_len):
    if messages_len == 1:
        return get_user_first_message_prompt()
    else:
        return get_user_prompt()


def capture_screenshot_in_a_folder(
    screenshots_dir: Path = Path("screenshots"), unique_id: str = None
):
    screenshot_filename = os.path.join(screenshots_dir, f"{unique_id}.png")
    capture_screen_with_cursor(screenshot_filename)
    return screenshot_filename


def save_labeled_pil_img_in_folder(
    annotated_pil_image: Image,
    screenshots_dir: Path = Path("screenshots"),
    unique_id: str = None,
):
    labeled_img_path = os.path.join(screenshots_dir, f"{unique_id}_labelled.png")
    annotated_pil_image.save(labeled_img_path)
    return labeled_img_path


def remove_code_block(content):
    if content.startswith("```json"):
        content = content[len("```json") :]
        if content.endswith("```"):
            content = content[: -len("```")]
    return content


def call_gpt_4_vision_preview_labeled(
    openai_client,
    messages,
    screenshots_dir: Path = Path("screenshots"),
    skip_som_draw_labels=[],
):
    """
    Calls the GPT-4 Vision Preview Labeled API to generate a response based on the given messages and objective.

    Args:
        openai_client (OpenAIClient): The OpenAI client object.
        messages (list): List of messages exchanged between the user and the assistant.
        screenshots_dir (Path, optional): The directory to save the screenshots. Defaults to "screenshots".
        skip_som_draw_labels (list, optional): List of labels to skip during image processing. Defaults to [].

    Returns:
        dict: The processed content generated by the GPT-4 Vision Preview Labeled API.
    """
    time.sleep(1)

    label_coordinates, img_base64_labeled = get_label_coordinates_and_base64_encoded_image(screenshots_dir, skip_som_draw_labels)
    user_prompt = get_relative_user_prompt(len(messages))
    vision_message = format_gpt4v_message(user_prompt, img_base64_labeled)
    messages.append(vision_message)
    content = get_response_from_gpt4v(
        openai_client,
        messages,
        temperature=config.openai_temperature,
        max_tokens=config.openai_max_tokens,
    )
    # print("[Intent Pilot][call_gpt_4_vision_preview_labeled] content", content)
    processed_content = process_model_response(messages, label_coordinates, content, "call_gpt_4_vision_preview_labeled")

    return processed_content

def call_ollama_vision_labeled(
    ollama_client,
    messages,
    screenshots_dir: Path = Path("screenshots"),
    skip_som_draw_labels=[],
):
    """
    Calls the Ollama API to generate a response based on the given messages and objective.

    Args:
        ollama_client (OllamaClient from LangChain): The Ollama client object from LangChain.
        messages (list): List of messages exchanged between the user and the assistant.
        screenshots_dir (Path, optional): The directory to save the screenshots. Defaults to "screenshots".
        skip_som_draw_labels (list, optional): List of labels to skip during image processing. Defaults to [].

    Returns:
        dict: The processed content generated by the Ollama API.
    """
    time.sleep(1)

    label_coordinates, img_base64_labeled = get_label_coordinates_and_base64_encoded_image(screenshots_dir, skip_som_draw_labels)
    user_prompt = get_relative_user_prompt(len(messages))

    # Context window is only 4000 tokens. Only the last two messages
    # + System prompt can be send together with the image to the model
    # without losing the system prompt: Remove index 1 and 2 to make room
    if len(messages) == 3:
        del messages[1]

    vision_message = format_ollama_message({"text": user_prompt, "image": img_base64_labeled})
    messages.append(vision_message)

    content = get_response_from_ollama(
        ollama_client,
        messages
    )

    # We remove the last message that contains the image
    # because the local model can not handle more than
    # one image on each prompt (Tested on MacBook Pro 32gb)
    del messages[-1]

    print("[Intent Pilot][call_ollama_vision] content", content)
    processed_content = process_model_response(messages, label_coordinates, content, "call_ollama_vision", "ollama")

    return processed_content

def get_label_coordinates_and_base64_encoded_image(screenshots_dir, skip_som_draw_labels):
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    img_id = uuid.uuid4()

    screenshot_filename = capture_screenshot_in_a_folder(screenshots_dir, img_id)
    pil_image_with_bboxes, label_coordinates = get_labeled_image(
        screenshot_filename, skip_labels=skip_som_draw_labels
    )
    labeled_img_path = save_labeled_pil_img_in_folder(
        pil_image_with_bboxes, screenshots_dir, img_id
    )
    img_base64_labeled = encode_image(labeled_img_path)
    return label_coordinates,img_base64_labeled

def process_model_response(messages, label_coordinates, content, log_prefix, model="gpt4v"):
    content = remove_code_block(content.strip())

    assistant_message = {"role": "assistant", "content": content}
    # For Ollama curly braces need to be replaced with double curly braces
    if model == "ollama":
        assistant_message = ("assistant", content.replace("{", "{{").replace("}", "}}"))
    messages.append(assistant_message)

    content = json.loads(content.replace("  ", ""))

    if config.verbose:
        print(
            f"{ANSI_BRIGHT_GREEN} [Intent Pilot][{log_prefix}] processed_content: {content} {ANSI_RESET}"
        )
    processed_content = merge_click_operations(label_coordinates, content)
    return processed_content

def calculate_center(bbox):
    return (bbox["xmin"] + bbox["xmax"]) / 2, (bbox["ymin"] + bbox["ymax"]) / 2


def process_click_operation(operation, label_coordinates):
    if operation.get("operation") == "click-text":
        text_bbox = extract_element_bbox(
            operation.get("text", None), label_coordinates["text"], flexible_search=True
        )
        x, y = calculate_center(text_bbox)
    elif operation.get("operation") == "click-icon":
        label_bbox = extract_element_bbox(
            int(operation.get("label", None)), label_coordinates["indices"]
        )
        x, y = calculate_center(label_bbox)
    else:
        return operation

    operation["operation"] = "click"
    operation["x"] = x
    operation["y"] = y
    return operation


def merge_click_operations(label_coordinates, content):
    processed_content = [
        process_click_operation(operation, label_coordinates) for operation in content
    ]
    return processed_content
