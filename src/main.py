import argparse
import io
import math
import os
import shutil
import sys
from contextlib import redirect_stdout
import time
import traceback

import fiona
import geopandas
from alive_progress import alive_bar, config_handler
import pandas

from apply_cdl_data_to_parcels import apply_cdl_data_to_parcels
from filter_spatial_within import filter_spatial_within
from reclassify_raster import PixelRemapSpecs
from regrid_parcels_gdb_to_shp import geodatabases_to_geopackage

class DualStream(io.StringIO):
  def __init__(self, file):
    super().__init__()
    self.file = file
    self.terminal = sys.stdout
    self.is_in_docker_image = os.path.exists('/.dockerenv')

  def write(self, message):
    super().write(message)  # Write to in-memory buffer
    if self.is_in_docker_image:
      self.terminal.write(message)  # Write to terminal
      self.terminal.flush()  # Flush terminal immediately
    self.file.write(message)  # Write to file
    self.file.flush()  # Flush file immediately

  def flush(self):
    super().flush()  # Flush the in-memory buffer
    self.terminal.flush()  # Flush terminal buffer
    self.file.flush()  # Flush file buffer


reclass_spec: PixelRemapSpecs = {
  254: { 'color': (0, 0, 0), 'name': 'background', 'original': [0] }, # we cannot have 0
  1: { 'color': (147, 105, 48), 'name': 'crops', 'original': list(range(1, 61)) + list(range(66, 81)) + list(range(195, 256) ) },
  2: { 'color': (100, 100, 100), 'name': 'idle', 'original': [61] },
  3: { 'color': (74, 59, 7), 'name': 'grassland', 'original': [62, 176] },
  4: { 'color': (53, 65, 22), 'name': 'forest', 'original': [63, 141, 142, 143] },
  5: { 'color': (78, 67, 27), 'name': 'shrubland', 'original': [64, 152] },
  6: { 'color': (50, 47, 36), 'name': 'barren', 'original': [65, 131] },
  10: { 'color': (195, 29, 20), 'name': 'developed', 'original': [82] },
  11: { 'color': (60, 32, 32), 'name': 'developed_open', 'original': [121] },
  12: { 'color': (106, 47, 31), 'name': 'developed_low', 'original': [122] },
  13: { 'color': (195, 29, 20), 'name': 'developed_med', 'original': [123] },
  14: { 'color': (139, 17, 11), 'name': 'developed_high', 'original': [124] },
  20: { 'color': (72, 93, 133), 'name': 'water', 'original': [83, 111, 112] },
  21: { 'color': (50, 103, 132), 'name': 'wetlands', 'original': [87, 190] },
  22: { 'color': (42, 45, 47), 'name': 'woody_wetlands', 'original': [190] },
  28: { 'color': (64, 76, 97), 'name': 'aquaculture', 'original': [92] },
  255: { 'color': (0, 0, 0), 'name': 'missing', 'original': [] }
}

if __name__ == '__main__':
  parser = argparse.ArgumentParser(description="Read a single parcel feature layer from an ESRI geodatabase, split it into chunks, calculate cropland data layer pixel coverage for each parcel, and the save to a GeoPackage.")
  parser.add_argument('--gdb_path', required=True, type=str, help="Path to the ESRI geodatabase containing parcel data.")
  parser.add_argument('--layer_name', required=True, type=str, help="Name of the feature layer.")
  parser.add_argument('--id_key', required=True, type=str, help="Column name of the unique identifier for the parcels. The column name will be truncated to 10 characters. Will be read as a string column. If the value is not unique or it is null, the row's ogs_fid or index will be used.")
  parser.add_argument('--output_gpkg', required=True, type=str, help="Path to the output GeoPackage.")
  parser.add_argument('--chunk_size', type=int, default=10000, help="Number of features per chunk (default is 1000).")
  parser.add_argument('--filter_layer_path', type=str, help="The file path to a shapefile to filter out features. The filter is a spatial within. Can be inverted with --invert-filter.")
  parser.add_argument('--cdls_folder_path', type=str, help="Path to folder containing the folders for each year of the Cropland Data Layer named with the format 'YYYY_30m_cdls'.")
  parser.add_argument('--cdls_aoi_shp_path', type=str, help="Path to a shapefile specifying the area of interest for the Cropland Data Layers. They will be cropped to the extent of this shapefile.")
  parser.add_argument('--invert-filter', type=bool, help="Invert the filter condition.")
  parser.add_argument('--summary_output_folder_path', type=str, default='./output', help="Folder to save the summary data.")
  parser.add_argument('--skip_remove_io', type=bool, help="Skip removing the input/output folders.")
  parser.add_argument('--skip_processing', type=bool, help="Skip processing the feature layer.")
  parser.add_argument('--skip_merge', type=bool, help="Skip merging the feature layers.")

  args = parser.parse_args()
    
  output_logfile = f'./logging/{args.layer_name}.log'
  if (not os.path.isdir(f'./logging')): os.makedirs(f'./logging')
  with open(output_logfile, 'w') as logfile:
    sys.stdout.flush()
    
    hp = math.floor(len(output_logfile) / 2)
    
    print(f'')
    print(f'┌───────────────── \033[37;43mLogging output to {output_logfile}\033[0m ─────────────────┐')
    print(f'│                                                     {" " * len(output_logfile)} │')
    print(f'│ Run \033[93;1mtail -F {output_logfile}\033[0m in a separate terminal to view progress │')
    print(f'│                                                     {" " * len(output_logfile)} │')
    print(f'│ {" " * hp}                 \033[93;2mCTRL + C\033[0m\033[2m to cancel\033[0m                  {" " * hp} │')
    print(f'└─────────────────────────────────────────────────────{"─" * len(output_logfile)}─┘')
    # print(f'Run \033[93;1mtail -F {output_logfile}' + " 2>&1 | perl -ne 'if (/file truncated/) {system 'clear'} else {print}'} | grep " + '"ERROR"' + "\033[0m in a separate terminal to view progress (clear when restarted)")
    with redirect_stdout(DualStream(logfile)):
      try:
        max_cols = 120
        config_handler.set_global(force_tty=True, max_cols=max_cols)
        config_handler.set_global(title_length=32)

        # print the start time
        start_time = time.time()
        print(f'\n\n\n\n\n\n\n\n\n\nStart time:\n  {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))}')
                
        # print all arguments in args
        print(f'\nArguments:')
        for arg in vars(args):
          print(f'  {arg}: {getattr(args, arg)}')
                
        print(f'\n{"─" * max_cols}\nChunking {args.layer_name} in {args.gdb_path} into {args.chunk_size}-feature chunks...')
        
        # remove working and ouput folders/paths if they exist
        if not args.skip_remove_io:
          if (os.path.isdir('./working')): shutil.rmtree('./working')
          if (os.path.isdir(args.summary_output_folder_path)):
            for item in os.listdir(args.summary_output_folder_path):
              if (os.path.isfile(os.path.join(args.summary_output_folder_path, item))): os.remove(os.path.join(args.summary_output_folder_path, item))
              else: shutil.rmtree(os.path.join(args.summary_output_folder_path, item))
          if (os.path.exists(args.output_gpkg)): os.remove(args.output_gpkg)
        
        if not args.skip_processing:
          # read the feature layer from the geodatabase
          with alive_bar(title='Reading feature layer from geodatabase', monitor=False) as bar:
            gdb_name = os.path.basename(args.gdb_path)
            gdf = geopandas.read_file(args.gdb_path, layer=args.layer_name, engine='pyogrio', use_arrow=True, fid_as_index=True, columns=[args.id_key, 'lat', 'lon'])
            gdf[args.id_key] = gdf[args.id_key].astype(str)
            gdf['INPUT_FID'] = gdf.index + 1
            gdf.reset_index(drop=True, inplace=True)

            # replace null values with the prefixed value from ogc_fid or index
            null_mask = gdf[args.id_key].isnull()
            if null_mask.any():  # check if there are any null entries
                if 'INPUT_FID' in gdf.columns:
                    gdf.loc[null_mask, args.id_key] = 'NULL[[INPUT_FID]]' + gdf.loc[null_mask, 'INPUT_FID'].astype(str)
                else:
                    gdf.loc[null_mask, args.id_key] = 'NULL[[index]]' + gdf.loc[null_mask].index.astype(str)

            # replace non-unique values of id_key with the prefixed value from ogc_fid or index
            non_unique_mask = gdf.duplicated(args.id_key, keep=False)
            if non_unique_mask.any():  # check if there are any non-unique entries
              if 'INPUT_FID' in gdf.columns:
                  gdf.loc[non_unique_mask, args.id_key] = gdf.loc[non_unique_mask, args.id_key] + '[[INPUT_FID]]' + gdf.loc[non_unique_mask, 'INPUT_FID'].astype(str)
              else:
                  gdf.loc[non_unique_mask, args.id_key] = gdf.loc[non_unique_mask, args.id_key] + '[[index]]' + gdf.loc[non_unique_mask].index.astype(str)

            # rename the id_key column to the first 10 characters
            gdf = gdf.rename(columns={ args.id_key: args.id_key[0:10] })

          # split the feature layer into chunks
          with alive_bar(title='Chunking feature layer', total=math.ceil(len(gdf) / int(args.chunk_size))) as bar:
            chunks = []
            for i in range(0, len(gdf), args.chunk_size):
              chunks.append(gdf.iloc[i:i + args.chunk_size])
              bar()
              
          # save each chunk into a different layer in the GeoPackage
          with alive_bar(title='Saving chunks to GeoPackage', total=len(chunks)) as bar:
            chunked_gpkg_path = f'./working/{gdb_name}/{args.layer_name}__chunked.gpkg'
            filtered_chunked_gpkg_path = f'./working/{gdb_name}/{args.layer_name}__chunked__filtered.gpkg'
            
            # create the folder for the GeoPackage
            if (not os.path.isdir(os.path.dirname(chunked_gpkg_path))):
              os.makedirs(os.path.dirname(chunked_gpkg_path))
            
            # save each chunk into a different layer in the GeoPackage
            for i, chunk in enumerate(chunks):
              layer_chunk = f'layer_{i + 1}'
              chunk.to_file(chunked_gpkg_path, layer=layer_chunk, driver='GPKG', append=True)
              bar()
                        
          # create a new geopackage without urban area parcels
          if (args.filter_layer_path):
            filter_spatial_within(
              input_layer_path=chunked_gpkg_path,
              filter_layer_path=args.filter_layer_path,
              output_layer_path=filtered_chunked_gpkg_path,
              invert=args.invert_filter,
              loop_print='\n' + '─' * max_cols + '\nFiltering (spatial within) for chunk "{chunk_name}" ({count}/{total})...'
            )
                  
          # create a list of the chunked layers by reading the GeoPackage
          gpkg_path = filtered_chunked_gpkg_path if args.filter_layer_path else chunked_gpkg_path
          chunk_names = fiona.listlayers(gpkg_path)
          
          # create temporary shapefile versions of each chunk since `apply_cdl_data_to_parcels` requires shapefiles
          print(f'\n{"─" * max_cols}')
          with alive_bar(len(chunk_names), title='Saving chunks to shapefiles') as bar:
            for chunk_name in chunk_names:
              chunk_gdf = geopandas.read_file(gpkg_path, layer=chunk_name, engine='pyogrio', use_arrow=True)
              chunk_gdf[args.id_key[0:10]] = chunk_gdf[args.id_key[0:10]].astype(str)
              chunk_gdf.to_file(f'./working/{gdb_name}/{args.layer_name}__{chunk_name}.shp')
              bar()
          
          # for each chunk, process the feature layer
          for index, chunk_name in enumerate(chunk_names):
            print(f'\n{"─" * max_cols}\nProcessing chunk "{chunk_name}" for summaries ({index + 1}/{len(chunk_names)})...')

            parcels_gpkg_output_path=f'{args.summary_output_folder_path}/chunked/{args.layer_name}__{chunk_name}__output.gpkg'
            os.makedirs(os.path.dirname(parcels_gpkg_output_path), exist_ok=True)
            
            apply_cdl_data_to_parcels(
              cropscape_input_folder=args.cdls_folder_path, # folder containing cropland data layer rasters folders
              area_of_interest_shapefile=args.cdls_aoi_shp_path, # shapefile defining area of interest
              clipped_rasters_folder='./working/clipped', # folder for rasters clipped to area of interest
              consolidated_rasters_folder='./working/consolidated', # folder for consolidated cropland data layer rasters
              reclass_spec=reclass_spec,
              parcels_shp_path=f'./working/{gdb_name}/{args.layer_name}__{chunk_name}.shp',
              id_key=args.id_key[:10],
              parcels_summary_file=f'{args.summary_output_folder_path}/chunked/{chunk_name}__summary_data.json',
              clipped_parcels_rasters_folder='./working/clipped_parcels_rasters',
              parcels_trajectories_file=f'{args.summary_output_folder_path}/chunked/{chunk_name}__trajectories.json',
              parcels_gpkg_output_path=parcels_gpkg_output_path,
              skip_raster_clipping_and_reclassifying=index > 0,
              skip_trajectories=True
            )

          for index, chunk_name in enumerate(chunk_names):
            print(f'\n{"─" * max_cols}\nProcessing chunk "{chunk_name}" for trajectories ({index + 1}/{len(chunk_names)})...')

            parcels_gpkg_output_path=f'{args.summary_output_folder_path}/chunked/{args.layer_name}__{chunk_name}__output.gpkg'
            
            apply_cdl_data_to_parcels(
              cropscape_input_folder=args.cdls_folder_path, # folder containing cropland data layer rasters folders
              area_of_interest_shapefile=args.cdls_aoi_shp_path, # shapefile defining area of interest
              clipped_rasters_folder='./working/clipped', # folder for rasters clipped to area of interest
              consolidated_rasters_folder='./working/consolidated', # folder for consolidated cropland data layer rasters
              reclass_spec=reclass_spec,
              parcels_shp_path=f'./working/{gdb_name}/{args.layer_name}__{chunk_name}.shp',
              id_key=args.id_key[:10],
              parcels_summary_file=f'{args.summary_output_folder_path}/chunked/{chunk_name}__summary_data.json',
              clipped_parcels_rasters_folder='./working/clipped_parcels_rasters',
              parcels_trajectories_file=f'{args.summary_output_folder_path}/chunked/{chunk_name}__trajectories.json',
              parcels_gpkg_output_path=parcels_gpkg_output_path,
              skip_raster_clipping_and_reclassifying=True,
              skip_summary_data=True
            )
        
        if not args.skip_merge:
          print(f'\n{"─" * max_cols}\nMerging chunked layers into "{args.output_gpkg}"...')

          # if chunk_names is not available, manually recreate it
          # by estimating names based on the number of chunks in ./working/{gdb_name}
          # (valid chunks end with '__output.gpkg')
          if args.skip_processing:
            gdb_name = os.path.basename(args.gdb_path)
            for item in os.listdir(f'./working/{gdb_name}'):
              chunk_names = []
              if item.endswith('__output.gpkg'):
                chunk_name = item.split('__')[1].replace('__output.gpkg', '')
                chunk_names.append(chunk_name)
                break

          # merge all the chunked layers into a single layer
          merged_counts_gdf = geopandas.GeoDataFrame()
          merged_trajectories_gdf = geopandas.GeoDataFrame()
          with alive_bar(2 * len(chunk_names), title='Merging chunked layers') as bar:
            for chunk_name in chunk_names:
              chunk_path = f'{args.summary_output_folder_path}/chunked/{args.layer_name}__{chunk_name}__output.gpkg'

              if (os.path.exists(chunk_path)):
                try:
                  chunk_counts_gdf = geopandas.read_file(chunk_path, layer='Parcels with CDL counts', engine='pyogrio', use_arrow=True)
                  chunk_counts_gdf[args.id_key[0:10]] = chunk_counts_gdf[args.id_key[0:10]].astype(str)
                  merged_counts_gdf = pandas.concat([merged_counts_gdf, chunk_counts_gdf], ignore_index=True)
                  bar()
                except:
                  print(f'Error reading {chunk_path} layer "Parcels with CDL counts"')
              
                try:
                  chunk_trajectories_gdf = geopandas.read_file(chunk_path, layer='Parcels with CDL pixel trajectories', engine='pyogrio', use_arrow=True)
                  chunk_trajectories_gdf[args.id_key[0:10]] = chunk_trajectories_gdf[args.id_key[0:10]].astype(str)
                  merged_trajectories_gdf = pandas.concat([merged_trajectories_gdf, chunk_trajectories_gdf], ignore_index=True)
                  bar()
                except:
                  print(f'Error reading {chunk_path} layer "Parcels with CDL pixel trajectories"')
                
            
          # save merged layers to the output GeoPackage
          with alive_bar(2, title='Saving merged layers', monitor=False) as bar:
            merged_counts_gdf.to_file(args.output_gpkg, layer='Parcels with CDL counts', driver='GPKG')
            bar()
            merged_trajectories_gdf.to_file(args.output_gpkg, layer='Parcels with CDL pixel trajectories', driver='GPKG')
            bar()

        print(f'\n{"─" * max_cols}\nDONE')
        print(f'  Total elapsed time: {time.strftime("%H:%M:%S", time.gmtime(time.time() - start_time))}')
        print(f'  Output saved to {args.output_gpkg}')

      except Exception as err:
        print(f'\n\n{err}\n' + ''.join(traceback.format_tb(err.__traceback__)))
        print(f'\nERROR:\n{err}\nPlease refer to the stack strace above for more information\n')
        print(f'Total elapsed time: {time.strftime("%H:%M:%S", time.gmtime(time.time() - start_time))}')
        sys.exit(f'There was an error. Please check the log file at {output_logfile} for more information.')

      except KeyboardInterrupt as err:
        print(f'\nProgram terminated by user via KeyboardInterrupt')
        print(f'Total elapsed time: {time.strftime("%H:%M:%S", time.gmtime(time.time() - start_time))}')
