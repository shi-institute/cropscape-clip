import os
import geopandas
import rasterio
import rasterio.mask
import rasterio.warp
from shapely.geometry import shape

INPUT_FOLDER = '/input'
CLIP_SHAPE = '/input/area_of_interest.shp'
OUTPUT_FOLDER = '/output'

dir_path = os.path.dirname(os.path.realpath(__file__))

# create output folder
if (not os.path.isdir(dir_path + OUTPUT_FOLDER)): 
  print('creating output folder...')
  os.mkdir(dir_path + OUTPUT_FOLDER)
  print('  ☑ Done')
  
# create clipped cropscape rasters for every raster in the input folder
for folder in sorted(os.listdir(dir_path + INPUT_FOLDER)):
  folder_path = dir_path + INPUT_FOLDER + '/' + folder
  if os.path.isdir(folder_path):
    for filename in sorted(os.listdir(folder_path)):
      file_path = folder_path + '/' + filename
      if filename.endswith("_30m_cdls.tif"):
        print(f'prcoessing {filename}...')
        year = filename[0:4]
        
        # open the raster and lock it in the filesystem while working on it
        raster = rasterio.open(file_path)
        
        # read the clip shapefile (.shp)
        clip_shp_original = geopandas.read_file(dir_path + CLIP_SHAPE)

        # reproject the clip shape to match the raster projection
        # because rasterio requires matching projections for masking (clipping)
        print('  ...matching projections...')
        reprojection_geometry = rasterio.warp.transform_geom(
          src_crs=clip_shp_original.crs,
          dst_crs=raster.crs,
          geom=clip_shp_original.geometry.values,
        )
        clip_shp_reprojected = clip_shp_original.set_geometry(
            [shape(geom) for geom in reprojection_geometry],
            crs=raster.crs,
        )
        
        # clip raster to shapefile and rewrite output metadata
        print('  ...clipping...')
        out_image, out_transform = rasterio.mask.mask(raster, clip_shp_reprojected.geometry.values, crop=True)
        out_meta = raster.meta.copy()
        out_meta.update({ 
                          "driver": "GTiff",
                          "height": out_image.shape[1],
                          "width": out_image.shape[2],
                          "transform": out_transform,
                          "nodata": 0
                        })
        
        # export the clipped raster with same colormap as the source raster
        print('  ...exporting...')
        with rasterio.open(f'{dir_path}{OUTPUT_FOLDER}/{year}_30m_cdls.tif', "w", **out_meta) as dest:
          dest.write(out_image)
          dest.write_colormap(1, raster.colormap(1))

        # remove the lock on the raster
        raster.close()
        print('  ☑ Done')

print('🏁 Finished')