import asyncio
import json
import re
import os
import time
import pandas as pd
from playwright.async_api import async_playwright, Response, TimeoutError
from datetime import datetime # Import datetime for date_extraction
from pathlib import Path # Import Path for file operations, as in your example

from shiny import App, ui, render, reactive
from shiny.types import FileInfo
import faicons


# Import for Plotly and ShinyWidgets
import plotly.express as px
import shinywidgets as sw

# Define appdir as in your example, though not strictly used for data loading here
appdir = Path(__file__).parent

# --- Playwright Scraper Logic ---

async def scrape_tiktok_profile(url: str, storage_state_path: str = None, headless_debug: bool = True):
    """
    Navigates to a TikTok profile URL and yields video data as it's extracted.
    Prioritizes extraction from embedded HTML/JavaScript, then processes
    intercepted 'api/post/item_list/' responses.
    Can load and save browser storage state to avoid CAPTCHAs/maintain session.

    Args:
        url (str): The URL of the TikTok profile to scrape.
        storage_state_path (str, optional): Path to a JSON file to save/load
                                            browser storage state (cookies, local storage).
                                            Defaults to None (no state persistence).
        headless_debug (bool): If False, runs Playwright in headed mode (visible browser)
                               for debugging. Defaults to True (headless).

    Yields:
        dict: A dictionary representing a single video post with extracted metrics.
    """
    intercepted_api_responses = [] # Store raw API responses temporarily
    seen_ids = set() # To prevent duplicate videos if found in multiple sources/API calls

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless_debug)
        
        context_options = {}
        if storage_state_path and os.path.exists(storage_state_path):
            print(f"\n⚠️🍪 Loading storage state from: {storage_state_path}")
            context_options['storage_state'] = storage_state_path
        
        context = await browser.new_context(**context_options)
        page = await context.new_page()

        async def handle_route(route):
            if re.search(r'api/post/item_list/', route.request.url):
                print(f"\n 📡 👀 Intercepting API request: {route.request.url}")
                await route.continue_()
                response = await route.request.response()
                if response:
                    try:
                        json_data = await response.json()
                        intercepted_api_responses.append(json_data) # Store for later processing
                        print(f"\n🎯 Successfully intercepted and stored raw JSON from {route.request.url}")
                    except Exception as e:
                        print(f"Could not parse JSON from {route.request.url}: {e}")
                else:
                    print(f"No response received for {route.request.url}")
            else:
                await route.continue_()

        await page.route(re.compile(r'api/post/item_list/'), handle_route)

        print(f"Navigating to {url}...")
        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
            print("Initial page load complete.")
            await page.wait_for_timeout(5000) # Give time for dynamic content

            # --- Strategy 1: Extract from embedded JSON in HTML (Primary, more reliable) ---
            print("Attempting to extract data from embedded HTML/JavaScript...")
            page_content = await page.content()
            
            match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>', page_content, re.DOTALL)
            
            if match:
                json_str = match.group(1)
                try:
                    universal_data = json.loads(json_str)
                    print("Successfully parsed __UNIVERSAL_DATA_FOR_REHYDRATION__.")
                    
                    user_detail_scope = universal_data.get('__DEFAULT_SCOPE__', {}).get('webapp.user-detail', {})
                    aweme_list_from_html = user_detail_scope.get('userInfo', {}).get('user', {}).get('awemeList', [])
                    
                    if not aweme_list_from_html:
                        aweme_list_from_html = user_detail_scope.get('itemInfo', {}).get('itemStruct', [])

                    if aweme_list_from_html:
                        print(f"SUCCESS: Found {len(aweme_list_from_html)} video items from embedded HTML.")
                        for item in aweme_list_from_html:
                            if isinstance(item, dict) and 'aweme_id' in item:
                                video_id = item.get('aweme_id', 'N/A')
                                # Ensure 'createTime' and 'author' keys exist before accessing
                                create_time = item.get('createTime')
                                author_unique_id = item.get('author', {}).get('uniqueId')

                                if video_id not in seen_ids:
                                    yield {
                                        "ID": video_id,
                                        "Description": item.get('desc', 'No description'),
                                        "playCount": item["statsV2"]["playCount"],
                                        "Likes": item.get('statistics', {}).get('digg_count', 'N/A'),
                                        "Comments": item.get('statistics', {}).get('comment_count', 'N/A'),
                                        "Shares": item.get('statistics', {}).get('share_count', 'N/A'),
                                        "bookmarks":item["statsV2"]["collectCount"],
                                        "repostCount":item["statsV2"]["repostCount"],
                                        # Convert timestamp to datetime object for sorting
                                        #"creation_date" : str(datetime.fromtimestamp(create_time)) if create_time is not None else None, # Check for None                                    
                                        #"creation_date" : pd.to_datetime(datetime.fromtimestamp(create_time)) if create_time is not None else None, # Check for None                                    
                                        #"creation_date" : pd.to_datetime(create_time, unit='s',format='%Y/%m/%d') if create_time is not None else None, # Check for None                                    
                                        "creation_date" : pd.to_datetime(create_time, unit='s') if create_time is not None else None, # Check for None                                    
                                        "video_url" : f"https://www.tiktok.com/@{author_unique_id}/video/{video_id}" if author_unique_id and video_id != 'N/A' else 'N/A',
                                        "creator_image" : item['author']['avatarThumb'],
                                        "video_cover" : item['video']['zoomCover']['720'],
                                        "video_duration":item['video']['duration'],
                                        "date_extraction": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                    }
                                    seen_ids.add(video_id)
                            else:
                                print(f"Warning: Unexpected item structure in awemeList from HTML: {item}")
                    else:
                        print("INFO: No 'awemeList' or similar found within the embedded HTML data using common paths.")

                except json.JSONDecodeError as e:
                    print(f"ERROR: Error decoding JSON from __UNIVERSAL_DATA_FOR_REHYDRATION__: {e}")
                except Exception as e:
                    print(f"ERROR: Error processing embedded HTML data: {e}")
            else:
                print("INFO: Could not find __UNIVERSAL_DATA_FOR_REHYDRATION__ script tag in HTML. Page structure might have changed.")
            # --- End Strategy 1 ---

            # Check for the "Refresh" button and click it if present (secondary action)
            refresh_button_selector = 'button[type="button"].emuynwa3.css-z9i4la-Button-StyledButton.ehk74z00'
            try:
                print("Checking for 'Refresh' button...")
                await page.wait_for_selector(refresh_button_selector, timeout=3000)
                print("Refresh button found. Clicking to reload the page...")
                await page.click(refresh_button_selector)
                await page.wait_for_load_state('networkidle', timeout=60000)
                await page.wait_for_timeout(5000)
            except TimeoutError:
                print("Refresh button not found or page loaded without it.")
            except Exception as e:
                print(f"An error occurred while interacting with the refresh button: {e}")

            # --- Attempt to scroll down to load more content (secondary action) ---
            print("Attempting to scroll down to load more content...")
            previous_height = -1
            max_scrolls = 3 
            scroll_count = 0
            while scroll_count < max_scrolls:
                current_height = await page.evaluate("document.body.scrollHeight")
                if current_height == previous_height:
                    print("No new content loaded after scrolling. Stopping scroll.")
                    break
                
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                print(f"Scrolled to bottom. Current height: {current_height}")
                await page.wait_for_timeout(2000)
                previous_height = current_height
                scroll_count += 1
            print("\n✅ Finished scrolling attempts.")
            # --- End scroll attempt ---

            # --- Strategy 2: Process intercepted API responses (Primary for certain data) ---
            print(f"Total raw API responses intercepted: {len(intercepted_api_responses)}. Attempting to process them with 'itemList'...")
            if not intercepted_api_responses:
                print("INFO: No raw API responses were intercepted for 'api/post/item_list/'.")

            for response_data in intercepted_api_responses:
                if 'itemList' in response_data and response_data['itemList']:
                    print(f"SUCCESS: Found 'itemList' with {len(response_data['itemList'])} items from API response.")
                    for item in response_data['itemList']:
                        if isinstance(item, dict):
                            video_id = item.get("id", 'N/A')
                            # Ensure 'createTime' and 'author' keys exist before accessing
                            create_time = item.get('createTime')
                            author_unique_id = item.get('author', {}).get('uniqueId')

                            if video_id not in seen_ids:
                                yield {
                                    "ID": video_id,
                                    "Description": item.get("desc", 'No description'),
                                    "playCount": item["statsV2"]["playCount"],
                                    "Likes": item.get("stats", {}).get("diggCount", 'N/A'),
                                    "Comments": item.get("stats", {}).get("commentCount", 'N/A'),
                                    "Shares": item.get("stats", {}).get("shareCount", 'N/A'),
                                    "bookmarks":item["statsV2"]["collectCount"],
                                    "repostCount":item["statsV2"]["repostCount"],
                                    # Convert timestamp to datetime object for sorting
                                    #"creation_date" : pd.to_datetime(datetime.fromtimestamp(create_time)) if create_time is not None else None, # Check for None
                                    #"creation_date" : pd.to_datetime(create_time, unit='s', format='%Y/%m/%d') if create_time is not None else None, # Check for None                                                                        
                                    "creation_date" : pd.to_datetime(create_time, unit='s') if create_time is not None else None, # Check for None                                                                        
                                    "video_url" : f"https://www.tiktok.com/@{author_unique_id}/video/{video_id}" if author_unique_id and video_id != 'N/A' else 'N/A',
                                    "creator_image" : item['author']['avatarThumb'],
                                    "video_cover" : item['video']['zoomCover']['480'],
                                    "video_duration":item['video']['duration'],
                                    "date_extraction": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                }
                                seen_ids.add(video_id)
                        else:
                            print(f"Warning: Unexpected item type in 'itemList': {type(item)}. Skipping.")
                else:
                    print("INFO: 'itemList' not found or empty in one of the raw API responses. This response might be a different type or structure.")
        
        except TimeoutError:
            print(f"ERROR: Navigation to {url} timed out. The page might not have loaded correctly or network was too slow.")
        except Exception as e:
            print(f"ERROR: An unexpected error occurred during navigation or {e}")

        # Save storage state if path is provided
        if storage_state_path:
            print(f"💾 Saving storage state to: {storage_state_path}")
            await context.storage_state(path=storage_state_path)
            print("💾 Storage state saved.")

        await browser.close()
        print("Browser closed.")

# --- Shiny App UI ---

app_ui = ui.page_fluid(
    ui.include_css(appdir / "styles.css"),
    ui.h2("TikTok Creator profiler"),
    ui.hr(),
    
    # Add Value Boxes at the top
    ui.layout_column_wrap(
        1/2, # Two columns
        ui.value_box(
            "Total Scraped Videos",
            ui.output_text("total_videos_value"), # Output for the count
            theme="bg-gradient-blue-purple",
            showcase=faicons.icon_svg("tiktok", width="75px")
        ),
        ui.value_box(
            "Total Likes (Latest Videos)", # Title for likes
            ui.output_text("latest_likes_value"), # Current total likes
            # Use shinywidgets output_widget for Plotly sparkline
            showcase=sw.output_widget("likes_sparkline"),
            #showcase=sw.output_widget("sparkline_2"),
            
            showcase_layout="bottom" # As per your example
            #theme="bg-gradient-green-yellow"
        )
    ),
    ui.hr(), # Separator between value boxes and sidebar layout

    ui.layout_sidebar(
        ui.sidebar(
            ui.h3("Input TikTok Profile URL"),
            ui.input_text(
                "tiktok_url",
                "TikTok URL:",
                value="https://www.tiktok.com/@greeicybaila",
                #value="https://www.tiktok.com/@realdonaldtrump",
                placeholder="e.g., https://www.tiktok.com/@username"
            ),
            ui.input_checkbox(
                "headless_mode",
                #"Run in Headless Mode (uncheck for debugging)",
                "Unselect to open the browser",
                value=True
            ),
            ui.input_action_button(
                "scrape_button",
                "Scrape Video Metrics",
                class_="btn-primary"
            ),
            ui.hr(),
            ui.markdown("""
                            **TikTok** data extractor
                        """),
          

            width="30%"
        ),
        ui.h3("TikTok Video Posts"),
        ui.output_data_frame("video_data_table"),
        ui.output_text("status_message")
    )
)

# --- Shiny App Server Logic ---

def server(input, output, session):
    # This list will accumulate data locally within the reactive effect
    current_scraped_items = [] 
    _status_message_rv = reactive.Value("Enter a TikTok URL and click 'Scrape Video Metrics'.")
    _status_message_rv.set("🚀 Scraping in progress...")

    # Define required columns for the DataFrame
    required_cols_df = ["ID", "Description", "playCount", "Likes", "Comments",
                        "Shares", "bookmarks", "repostCount",
                        "creation_date", "video_url",
                        "creator_image", "video_cover", "video_duration", "date_extraction"]

    # Reactive value to hold the currently scraped data for the table
    scraped_video_df_rv = reactive.Value(pd.DataFrame(columns=required_cols_df))
    
    # New reactive value for sparkline data: stores dicts with 'date' and 'likes'
    likes_for_sparkline_rv = reactive.Value([])
    # New reactive value for total likes
    total_likes_rv = reactive.Value(0)
    # New reactive value for total videos count
    total_videos_count_rv = reactive.Value(0)


    @reactive.Effect
    @reactive.event(input.scrape_button)
    async def _():
        url = input.tiktok_url()
        if not url:
            _status_message_rv.set("Please enter a TikTok URL.")
            return

        _status_message_rv.set("🚀 Scraping in progress... This may take a moment.")
        # Clear all reactive values and lists at the start of a new scrape
        current_scraped_items.clear() 
        scraped_video_df_rv.set(pd.DataFrame(columns=required_cols_df)) 
        likes_for_sparkline_rv.set([]) # Clear sparkline data
        total_likes_rv.set(0) 
        total_videos_count_rv.set(0)

        
        with ui.Progress(min=0, max=100) as p:
            p.set(10, message="Starting scraper...")
            
            current_dir = os.path.dirname(os.path.abspath(__file__))
            storage_file_path = os.path.join(current_dir, "tiktok_session_state.json")
            output_metrics_file_path = os.path.join(current_dir, "scraped_video_metrics.json")

            try:
                p.set(30, message="Navigating and extracting data...")
                # Iterate over the async generator
                async for video_item in scrape_tiktok_profile(
                    url, 
                    storage_state_path=storage_file_path,
                    headless_debug=input.headless_mode()
                ):
                    current_scraped_items.append(video_item)
                    
                    # Update sparkline data and total likes
                    likes_val = video_item.get("Likes")
                    creation_date_val = video_item.get("creation_date") # Get datetime object
                    
                    try:
                        numeric_likes = int(likes_val) # Try converting to int
                        if isinstance(creation_date_val, datetime):
                            current_likes_data = likes_for_sparkline_rv.get()
                            # Store a dictionary with date and likes for sorting
                            current_likes_data.append({"date": creation_date_val, "likes": numeric_likes})
                            likes_for_sparkline_rv.set(current_likes_data)
                            total_likes_rv.set(total_likes_rv.get() + numeric_likes)
                            print(f"DEBUG: Added to sparkline data: Date={creation_date_val}, Likes={numeric_likes}") # Debug print
                            print(f"DEBUG: Current likes_for_sparkline_rv: {likes_for_sparkline_rv.get()}") # Debug print
                        else:
                            print(f"Warning: Invalid creation_date type: {type(creation_date_val)}. Skipping for sparkline/total likes.")
                    except (ValueError, TypeError):
                        print(f"Warning: Non-numeric likes value encountered: {likes_val}. Skipping for sparkline/total likes.")

                    # Update total videos count
                    total_videos_count_rv.set(len(current_scraped_items))

                    # Create a NEW DataFrame object from the updated list
                    df_to_set = pd.DataFrame(current_scraped_items, columns=required_cols_df)
                    
                    # Ensure all expected columns are present, fill with N/A if missing
                    # Convert 'creation_date' column to string for display if it's datetime objects
                    if 'creation_date' in df_to_set.columns:
                        df_to_set['creation_date'] = df_to_set['creation_date'].apply(
                            lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if isinstance(x, datetime) else 'N/A'
                        )

                    for col in required_cols_df:
                        if col not in df_to_set.columns:
                            df_to_set[col] = "N/A"
                    df_to_set = df_to_set[required_cols_df] # Reorder and select only required columns
                    
                    # Update the reactive value, which will trigger the render.data_frame
                    scraped_video_df_rv.set(df_to_set)
                    
                    p.set(50 + (len(current_scraped_items) % 50), message=f"Scraped {len(current_scraped_items)} videos...")
                    _status_message_rv.set(f"📊 Scraped {len(current_scraped_items)} videos so far...")
                    
                    # Yield control to the event loop to allow UI to update
                    await asyncio.sleep(0.01) 

                if current_scraped_items:
                    _status_message_rv.set(f"✅ Successfully scraped and displayed {len(current_scraped_items)} video posts.")
                    
                    # --- Start of JSON serialization fix ---
                    # Create a copy of the items and convert datetime objects to strings for JSON serialization
                    json_serializable_items = []
                    for item in current_scraped_items:
                        serializable_item = item.copy() # Create a shallow copy
                        if isinstance(serializable_item.get("creation_date"), datetime):
                            serializable_item["creation_date"] = serializable_item["creation_date"].strftime('%Y-%m-%d %H:%M:%S')
                        json_serializable_items.append(serializable_item)

                    with open(output_metrics_file_path, 'w', encoding='utf-8') as f:
                        json.dump(json_serializable_items, f, indent=2, ensure_ascii=False)
                    # --- End of JSON serialization fix ---

                    print(f"Extracted video metrics saved to {output_metrics_file_path}")
                else:
                    _status_message_rv.set("❌ No video data found or an error occurred during scraping. Check console for details.")

            except Exception as e:
                _status_message_rv.set(f"An error occurred: {e}")
                print(f"Shiny App Error: {e}")
            finally:
                p.set(100, message="Done.")

    @output
    @render.data_frame
    def video_data_table():
        df = scraped_video_df_rv.get()        
        #df["video_cover"] = df["video_cover"].apply(lambda url: f'<img src="{url}" width="80">')
        df["video_cover"] = [ui.tags.img(src=url, width="75px") for url in df["video_cover"]]
        df["Description"] = [ui.tags.a(name, href=url, target="_blank") for name, url in zip(df["Description"], df["video_url"])]

        df = df[["video_cover", "Description", "playCount", "Likes", "Comments",
                        "Shares", "bookmarks", "repostCount", "video_duration",
                        "creation_date", 
                        #"video_url", "creator_image", 
                        "date_extraction", "ID"
                        ]]
        
        # Convert video_cover URLs to HTML <img> tags
        #df["video_cover"] = df["video_cover"].apply(lambda url: f'<img src="{url}" width="80">')
        #df_image = DataGrid(df)
        #return df_image
        
        '''
        render.DataGrid(
            df,
            # This is the key: set the column_formats to 'html' for the 'Image' column
            column_formats={
                "creator_image": render.ColumnFormat.html()
            }
            # Optional: adjust column widths
            
            #column_widths={
            #    "Product": "200px",
            #    "Price": "100px",
            #    "Image": "80px" # Give enough space for the image
            #}
        )
        '''
        return df
    
    

    @output
    @render.text
    def status_message():
        return _status_message_rv.get()

    # Outputs for Value Boxes
    @output
    @render.text
    def total_videos_value():
        return str(total_videos_count_rv.get()) 

    @output
    @render.text
    def latest_likes_value():
        return f"{total_likes_rv.get():,}" 

    @sw.render_widget # Use shinywidgets render_widget for Plotly
    def likes_sparkline():
        # Get the data, sort it by date, and then create a Plotly figure
        data = likes_for_sparkline_rv.get()
        #print(f"DEBUG: Data for sparkline before DataFrame conversion: {data}") # Debug print
        if not data:
            print("DEBUG: No data for sparkline, returning empty figure.") # Debug print
            # Return an empty figure if no data to prevent errors
            return px.line(pd.DataFrame(columns=["date", "likes"]))
        
        # Convert to DataFrame for Plotly and sort by date
        #print("++++ DATA", data)
        df_sparkline = pd.DataFrame(data)
        df_sparkline = df_sparkline.sort_values(by="date")
        df_sparkline['date'] = pd.to_datetime(df_sparkline['date'])
        df_sparkline.to_csv("tiktok_data.csv", index=None, header=True)
        df_sparkline = pd.read_csv(appdir / "tiktok_data.csv")
        #df_sparkline['date'] = df_sparkline['date'].dt.normalize()
        #print(f"DEBUG: DataFrame for sparkline: \n{df_sparkline}") # Debug print
        #breakpoint()
        # fig = px.line(df, x='TimestampColumn', y='Value1', title='Value1 Over Time')
        #fig = px.line(df_sparkline, x="date", y="likes")
        
        import plotly.graph_objects as go
        '''
        fig = go.Figure(
            data=[
                go.Scatter(
                    x=df_sparkline['date'],
                    y=df_sparkline['likes'], # Use your actual 'likes' data here
                    mode='lines',
                    hovertemplate='date=%{x}<br>likes=%{y}<extra></extra>',
                    line={'color': '#636efa', 'dash': 'solid'},
                    fillcolor="rgba(64,110,241,0.2)",
                    marker={'symbol': 'circle'},
                    showlegend=False,
                    name=''
                )
            ],
            layout=go.Layout(
                title={'text': 'Likes Over Time'}, # Added a title for better clarity
                xaxis={'title': {'text': 'date'}},
                yaxis={'title': {'text': 'likes'}},
                margin={'t': 60},
                legend={'tracegroupgap': 0}
                # template: '...' is often replaced by a specific template name
                # e.g., 'plotly_white', 'plotly_dark', 'ggplot2', 'seaborn', 'simple_white'
            )
        )
        '''
        
        
        fig = px.line(df_sparkline, x="date", y="likes")
        fig.update_traces(
            line_color="#87CEEB", # Sky Blue
            line_width=1,
            fill="tozeroy",
            fillcolor="rgba(135, 206, 235, 0.5)", # 20% opacity Sky Blue
            hoverinfo="x+y", # Show both x (date) and y (likes) on hover
        )
        fig.update_xaxes(
            visible=False,
            showgrid=False,
            type='date' # Explicitly set x-axis type to date
        )
        fig.update_yaxes(visible=False, showgrid=False)
        fig.update_layout(
            height=100, # Example height
            hovermode="x unified", # Use unified hovermode for better UX on sparklines
            margin=dict(t=0, r=0, l=0, b=0),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        return fig
    
    @sw.render_widget
    def sparkline_2():
        data = likes_for_sparkline_rv.get()
        df_sparkline = pd.DataFrame(data)
        df_sparkline.to_csv("tiktok_data.csv", index=None, header=True)
        
        try:
            df_sparkline = pd.read_csv(appdir / "tiktok_data.csv")
        except:
            #if not data:
            print("DEBUG: No data for sparkline, returning empty figure.") # Debug print
            # Return an empty figure if no data to prevent errors
            return px.line(pd.DataFrame(columns=["date", "likes"]))
            
        fig = px.line(df_sparkline, x="date", y="likes")
        fig.update_traces(
            line_color="#406EF1",
            line_width=1,
            fill="tozeroy",
            fillcolor="rgba(64,110,241,0.2)",
            hoverinfo="y",
        )
        fig.update_xaxes(visible=False, showgrid=False)
        fig.update_yaxes(visible=False, showgrid=False)
        fig.update_layout(
            height=100,
            hovermode="x",
            margin=dict(t=0, r=0, l=0, b=0),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        return fig

# --- Run the Shiny App ---
app = App(app_ui, server)
